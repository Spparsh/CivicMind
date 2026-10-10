"""CivicMind — infrastructure assets and risk findings.

Access policy
  GET  /api/assets, /api/assets/{id}, /api/map/assets   officer, admin
  POST /api/assets                                      admin
  POST /api/assets/import                               admin (GeoJSON FeatureCollection of Points)
  POST /api/assets/{id}/maintenance                     officer, admin
  POST /api/complaints/{id}/asset-match                 officer, admin (confirm / reject a match)
  GET  /api/risk/findings                               officer, admin
  POST /api/risk/scan                                   officer, admin
  POST /api/risk/findings/{id}/review                   officer, admin
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import require_officer, require_admin
from config import ASSET_TYPES
from database import query, db, now
from services import assets as asset_svc, risk, clustering

router = APIRouter(prefix="/api", tags=["infrastructure"])


class AssetIn(BaseModel):
    asset_code: str = Field(max_length=32)
    asset_type: str = Field(max_length=30)
    name: str | None = Field(default=None, max_length=120)
    latitude: float
    longitude: float
    criticality: str = Field(default="medium", max_length=10)
    criticality_reason: str | None = Field(default=None, max_length=200)
    operational_status: str = Field(default="operational", max_length=20)
    inspection_interval_days: int | None = None
    last_inspected_at: str | None = Field(default=None, max_length=25)
    data_source: str = Field(max_length=120)
    is_synthetic: bool = False


class ImportIn(BaseModel):
    geojson: dict
    data_source: str = Field(min_length=2, max_length=120)
    is_synthetic: bool = False


class MaintenanceIn(BaseModel):
    event_type: str = Field(max_length=20)
    performed_at: str = Field(max_length=25)
    outcome: str | None = Field(default=None, max_length=200)
    performed_by: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=500)
    data_source: str = Field(default="Officer entry", max_length=120)


class MatchReviewIn(BaseModel):
    status: str = Field(pattern="^(confirmed|rejected)$")


class ReviewIn(BaseModel):
    status: str = Field(pattern="^(open|acknowledged|dismissed)$")
    note: str | None = Field(default=None, max_length=300)


def _asset_error(e):
    raise HTTPException(422, {"message": str(e)})


@router.get("/assets/types")
def asset_types(user: dict = Depends(require_officer)):
    return [{"type": k, **v} for k, v in ASSET_TYPES.items()]


@router.get("/assets")
def list_assets(asset_type: str | None = Query(None, max_length=30), user: dict = Depends(require_officer)):
    return asset_svc.list_assets(asset_type)


@router.get("/assets/{asset_id}")
def get_asset(asset_id: int, user: dict = Depends(require_officer)):
    a = asset_svc.asset_detail(asset_id)
    if not a:
        raise HTTPException(404, "Asset not found")
    return a


@router.post("/assets")
def create_asset(body: AssetIn, user: dict = Depends(require_admin)):
    try:
        aid = asset_svc.create_asset(body.model_dump())
    except asset_svc.AssetError as e:
        _asset_error(e)
    risk.safe_scan()
    return {"id": aid}


@router.post("/assets/import")
def import_assets(body: ImportIn, user: dict = Depends(require_admin)):
    try:
        summary = asset_svc.import_geojson(body.geojson, body.data_source.strip(), body.is_synthetic)
    except asset_svc.AssetError as e:
        _asset_error(e)
    with db() as conn:
        conn.execute("INSERT INTO activity (actor, actor_id, action, detail) VALUES (?,?,?,?)",
                     (user["name"], user["id"], "assets_imported",
                      f"GeoJSON import: {summary['imported']} imported, {len(summary['duplicates'])} "
                      f"duplicate, {len(summary['rejected'])} rejected"))
    risk.safe_scan()
    return summary


@router.post("/assets/{asset_id}/maintenance")
def add_maintenance(asset_id: int, body: MaintenanceIn, user: dict = Depends(require_officer)):
    if not query("SELECT 1 FROM assets WHERE id=?", (asset_id,), one=True):
        raise HTTPException(404, "Asset not found")
    try:
        m = asset_svc.validate_maintenance(body.model_dump())
    except asset_svc.AssetError as e:
        _asset_error(e)
    with db(immediate=True) as conn:
        mid = asset_svc.add_maintenance(conn, asset_id, m, recorded_by=user["id"])
    risk.safe_scan()
    return {"id": mid}


@router.post("/complaints/{complaint_id}/asset-match")
def review_match(complaint_id: int, body: MatchReviewIn, user: dict = Depends(require_officer)):
    with db(immediate=True) as conn:
        cur = conn.execute("UPDATE complaint_assets SET status=?, reviewed_by=?, updated_at=? WHERE complaint_id=?",
                           (body.status, user["id"], now(), complaint_id))
        if cur.rowcount != 1:
            raise HTTPException(404, "No asset match for this complaint")
        inc = conn.execute("SELECT incident_id FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        if inc and inc["incident_id"]:
            clustering.refresh_incident(conn, inc["incident_id"])
    risk.safe_scan()
    return {"ok": True, "status": body.status}


@router.get("/map/assets")
def map_assets(user: dict = Depends(require_officer)):
    return asset_svc.list_assets()


@router.get("/risk/findings")
def findings(status: str = Query("active", pattern="^(active|open|acknowledged|dismissed|cleared|all)$"),
             user: dict = Depends(require_officer)):
    sql = """SELECT f.*, a.asset_code, a.asset_type, a.latitude AS asset_lat, a.longitude AS asset_lng,
                    i.public_id AS incident_public_id, i.centroid_lat, i.centroid_lng
             FROM risk_findings f LEFT JOIN assets a ON a.id=f.asset_id
             LEFT JOIN incidents i ON i.id=f.incident_id"""
    params = ()
    if status == "active":
        sql += " WHERE f.status IN ('open','acknowledged')"
    elif status != "all":
        sql += " WHERE f.status=?"
        params = (status,)
    sql += """ ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
               f.detected_at DESC LIMIT 300"""
    return [risk.finding_out(r) for r in query(sql, params)]


@router.post("/risk/scan")
def scan(user: dict = Depends(require_officer)):
    return risk.scan()


@router.post("/risk/findings/{finding_id}/review")
def review(finding_id: int, body: ReviewIn, user: dict = Depends(require_officer)):
    with db(immediate=True) as conn:
        cur = conn.execute("""UPDATE risk_findings SET status=?, reviewed_by=?, reviewed_at=?, review_note=?,
                              updated_at=? WHERE id=? AND status != 'cleared'""",
                           (body.status, user["id"], now(), (body.note or "").strip() or None, now(), finding_id))
        if cur.rowcount != 1:
            raise HTTPException(409, "Finding not found or already cleared")
        f = conn.execute("SELECT incident_id, explanation FROM risk_findings WHERE id=?", (finding_id,)).fetchone()
        conn.execute("INSERT INTO activity (actor, actor_id, action, detail, incident_id) VALUES (?,?,?,?,?)",
                     (user["name"], user["id"], "finding_reviewed",
                      f"Risk finding #{finding_id} marked {body.status}", f["incident_id"]))
    return {"ok": True}
