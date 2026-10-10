"""CivicMind — utility network API (contract: docs/CIVICMIND_INTEGRATION_CONTRACT.md).

Access policy — utility topology, contacts and operational details are NEVER civilian data:
  GET  /api/utility/meta                                   officer, admin
  GET  /api/utility/assets?bbox=&utility_types=&...        officer, admin  (GeoJSON FeatureCollection)
  GET  /api/utility/assets/{asset_id}                      officer, admin
  GET  /api/utility/assets/{asset_id}/trace                officer, admin  (deterministic)
  GET  /api/utility/assets/{asset_id}/isolation            officer, admin  (candidates only)
  GET  /api/utility/assets/{asset_id}/history              officer, admin
  GET  /api/utility/assets/{asset_id}/contacts             officer, admin
  POST /api/utility/assets/{asset_id}/copilot              officer, admin  (grounded Q&A, read-only)
  GET  /api/incidents/{incident_id}/utility-context        officer, admin
  POST /api/incidents/{incident_id}/utility-assets/{asset_id}/review   officer, admin
  POST /api/incidents/{incident_id}/briefing               officer, admin
  GET  /api/utility/findings   POST /api/utility/findings/scan   POST /api/utility/findings/{id}/review
  POST /api/utility/import/preview   POST /api/utility/import/confirm   GET /api/utility/import/jobs   admin
  POST /api/utility/records/verify                         admin
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import require_officer, require_admin
from database import query, db, now
from routes.common import raise_from
from routes.incidents import _find
from services.utility_network import copilot, findings, graph, importer, matching, model, registry

router = APIRouter(prefix="/api", tags=["utility-network"])
MAX_BBOX_DEG = 1.0


def _asset_or_404(asset_id):
    a = registry.get_row(asset_id)
    if not a:
        raise HTTPException(404, "Utility asset not found")
    return a


def _incident_or_404(incident_id):
    inc = _find(incident_id)
    if not inc:
        raise HTTPException(404, "Incident not found")
    return inc


@router.get("/utility/meta")
def meta(user: dict = Depends(require_officer)):
    counts = {r["utility_type"]: r["c"] for r in query(
        "SELECT utility_type, COUNT(*) c FROM utility_assets GROUP BY utility_type")}
    return {
        "utility_types": [{"key": k, "label": v["label"], "color": v["color"], "flow": v["flow"],
                           "isolation_applicable": bool(v["control_types"]), "count": counts.get(k, 0)}
                          for k, v in model.UTILITY_TYPES.items()],
        "provenance_classes": [{"key": k, "label": v} for k, v in model.PROVENANCE_LABELS.items()],
        "by_provenance": query("SELECT provenance_class, COUNT(*) c FROM utility_assets GROUP BY provenance_class"),
        "by_verification": query("SELECT verification_status, COUNT(*) c FROM utility_assets GROUP BY verification_status"),
        "connections": query("SELECT COUNT(*) c FROM utility_connections", one=True)["c"],
        "contacts": query("SELECT COUNT(*) c FROM utility_contacts", one=True)["c"],
        "synthetic_assets": query("SELECT COUNT(*) c FROM utility_assets WHERE is_synthetic=1", one=True)["c"],
    }


@router.get("/utility/assets")
def assets(bbox: str | None = Query(None, max_length=120), utility_types: str | None = Query(None, max_length=120),
           status: str | None = Query(None, max_length=20), provenance: str | None = Query(None, max_length=30),
           verification: str | None = Query(None, max_length=20), limit: int = Query(2000, ge=1, le=5000),
           user: dict = Depends(require_officer)):
    box = None
    if bbox:
        try:
            box = [float(x) for x in bbox.split(",")]
            if len(box) != 4:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "bbox must be minLng,minLat,maxLng,maxLat") from None
        if not (box[0] < box[2] and box[1] < box[3]) or box[2] - box[0] > MAX_BBOX_DEG or box[3] - box[1] > MAX_BBOX_DEG:
            raise HTTPException(422, f"bbox must be ordered and at most {MAX_BBOX_DEG}° on each side")
    types = [t for t in (utility_types or "").split(",") if t]
    if any(t not in model.UTILITY_TYPES for t in types):
        raise HTTPException(422, "Unknown utility type")
    return registry.list_features(box, types or None, status, provenance, verification, limit)


@router.get("/utility/assets/{asset_id}")
def asset_detail(asset_id: str, user: dict = Depends(require_officer)):
    return registry.detail(_asset_or_404(asset_id))


@router.get("/utility/assets/{asset_id}/trace")
def trace(asset_id: str, direction: str = Query("both", pattern="^(upstream|downstream|both)$"),
          max_hops: int = Query(graph.DEFAULT_MAX_HOPS, ge=1, le=graph.MAX_HOPS_LIMIT),
          user: dict = Depends(require_officer)):
    _asset_or_404(asset_id)
    return graph.trace(asset_id, direction, max_hops)


@router.get("/utility/assets/{asset_id}/isolation")
def isolation(asset_id: str, max_hops: int = Query(graph.DEFAULT_MAX_HOPS, ge=1, le=graph.MAX_HOPS_LIMIT),
              user: dict = Depends(require_officer)):
    _asset_or_404(asset_id)
    return graph.isolation(asset_id, max_hops)


@router.get("/utility/assets/{asset_id}/history")
def history(asset_id: str, user: dict = Depends(require_officer)):
    return registry.history(_asset_or_404(asset_id))


@router.get("/utility/assets/{asset_id}/contacts")
def contacts(asset_id: str, user: dict = Depends(require_officer)):
    return registry.contacts_for(_asset_or_404(asset_id))


class CopilotIn(BaseModel):
    question: str = Field(min_length=3, max_length=300)


@router.post("/utility/assets/{asset_id}/copilot")
def ask(asset_id: str, body: CopilotIn, user: dict = Depends(require_officer)):
    _asset_or_404(asset_id)
    return copilot.answer(asset_id, body.question)


@router.get("/incidents/{incident_id}/utility-context")
def utility_context(incident_id: str, user: dict = Depends(require_officer)):
    return matching.context(_incident_or_404(incident_id))


class LinkReviewIn(BaseModel):
    status: str = Field(pattern="^(confirmed|rejected|candidate)$")


@router.post("/incidents/{incident_id}/utility-assets/{asset_id}/review")
def review_link(incident_id: str, asset_id: str, body: LinkReviewIn, user: dict = Depends(require_officer)):
    inc = _incident_or_404(incident_id)
    if not matching.review(inc["id"], asset_id, body.status, user):
        raise HTTPException(404, "Utility asset not found")
    findings.safe_scan()
    return matching.context(inc)


@router.post("/incidents/{incident_id}/briefing")
def briefing(incident_id: str, user: dict = Depends(require_officer)):
    return copilot.briefing(_incident_or_404(incident_id))


@router.get("/utility/findings")
def list_findings(status: str = Query("active", pattern="^(active|open|acknowledged|dismissed|cleared|all)$"),
                  asset_id: str | None = Query(None, max_length=64), user: dict = Depends(require_officer)):
    return findings.list_findings(status, asset_id)


@router.post("/utility/findings/scan")
def scan(user: dict = Depends(require_officer)):
    return findings.scan()


class FindingReviewIn(BaseModel):
    status: str = Field(pattern="^(open|acknowledged|dismissed)$")
    note: str | None = Field(default=None, max_length=300)


@router.post("/utility/findings/{finding_id}/review")
def review_finding(finding_id: int, body: FindingReviewIn, user: dict = Depends(require_officer)):
    with db(immediate=True) as conn:
        cur = conn.execute("""UPDATE utility_findings SET status=?, reviewed_by=?, reviewed_at=?, review_note=?,
                              updated_at=? WHERE id=? AND status != 'cleared'""",
                           (body.status, user["id"], now(), (body.note or "").strip() or None, now(), finding_id))
        if cur.rowcount != 1:
            raise HTTPException(409, "Finding not found or already cleared")
        conn.execute("INSERT INTO activity (actor, actor_id, action, detail, created_at) VALUES (?,?,?,?,?)",
                     (user["name"], user["id"], "utility_finding_reviewed",
                      f"Utility finding #{finding_id} marked {body.status}", now()))
    return {"ok": True}


# --- import (admin) ----------------------------------------------------------------
class SourceIn(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    authority: str | None = Field(default=None, max_length=160)
    source_url: str | None = Field(default=None, max_length=2048)
    file_name: str | None = Field(default=None, max_length=200)
    license_notes: str | None = Field(default=None, max_length=500)
    provenance_class: str = Field(default="approximate_unverified", max_length=30)
    is_synthetic: bool = False


class PreviewIn(BaseModel):
    format: str = Field(pattern="^(geojson|csv)$")
    content: str = Field(max_length=importer.MAX_CONTENT_CHARS)
    source: SourceIn | None = None
    snapshot_id: int | None = None


@router.post("/utility/import/preview")
def import_preview(body: PreviewIn, user: dict = Depends(require_admin)):
    try:
        if body.snapshot_id:
            src = importer.source_from_snapshot(body.snapshot_id)
            if body.source and body.source.file_name:
                src["file_name"] = body.source.file_name
        else:
            if not body.source or not (body.source.name or "").strip():
                raise importer.ImportError_("Describe the data source (name) or pick a reviewed source snapshot.")
            src = body.source.model_dump() | {"source_type": "file_import"}
            if src["is_synthetic"]:
                src["provenance_class"] = "synthetic_demo"
        return importer.preview(user, body.format, body.content, src)
    except importer.ImportError_ as e:
        raise_from(e)


class ConfirmIn(BaseModel):
    job_code: str = Field(min_length=6, max_length=40)


@router.post("/utility/import/confirm")
def import_confirm(body: ConfirmIn, user: dict = Depends(require_admin)):
    try:
        res = importer.confirm(user, body.job_code)
    except importer.ImportError_ as e:
        raise_from(e)
    findings.safe_scan()
    return res


@router.get("/utility/import/jobs")
def import_jobs(user: dict = Depends(require_admin)):
    return importer.jobs()


class RollbackIn(BaseModel):
    job_code: str = Field(min_length=6, max_length=40)
    confirm: str = ""


@router.post("/utility/import/rollback")
def import_rollback(body: RollbackIn, user: dict = Depends(require_admin)):
    """Remove the records one committed import created — only if nothing else depends on them."""
    if body.confirm != "ROLLBACK":
        raise HTTPException(422, "Type ROLLBACK to confirm.")
    try:
        res = importer.rollback(user, body.job_code)
    except importer.ImportError_ as e:
        raise_from(e)
    findings.safe_scan()
    return res


class ConnectionIn(BaseModel):
    from_asset_id: str = Field(min_length=2, max_length=64)
    to_asset_id: str = Field(min_length=2, max_length=64)
    connection_type: str = Field(default="feeds", max_length=20)
    direction: str = Field(default="unknown", max_length=20)
    notes: str = Field(min_length=5, max_length=200)


@router.post("/utility/connections")
def add_connection(body: ConnectionIn, user: dict = Depends(require_admin)):
    """Record one connection by hand (e.g. from an as-built drawing). Stored unverified; never inferred."""
    if body.connection_type not in model.CONNECTION_TYPES or body.direction not in model.DIRECTIONS:
        raise HTTPException(422, "Unknown connection type or direction")
    f, t = registry.get_row(body.from_asset_id), registry.get_row(body.to_asset_id)
    if not f or not t:
        raise HTTPException(404, "Both assets must exist in the registry")
    if f["id"] == t["id"]:
        raise HTTPException(422, "An asset cannot connect to itself")
    if f["utility_type"] != t["utility_type"]:
        raise HTTPException(422, "Connections must join assets of the same utility network")
    cid = f"UC-{f['asset_id']}-{t['asset_id']}"[:64]
    with db(immediate=True) as conn:
        if conn.execute("""SELECT 1 FROM utility_connections WHERE (from_asset=? AND to_asset=?) OR (from_asset=? AND to_asset=?)""",
                        (f["id"], t["id"], t["id"], f["id"])).fetchone():
            raise HTTPException(409, "A connection between these assets is already recorded")
        conn.execute("""INSERT INTO utility_connections (connection_id, from_asset, to_asset, connection_type, direction,
                        provenance_class, verification_status, is_synthetic, notes, created_at)
                        VALUES (?,?,?,?,?, 'approximate_unverified', 'unverified', ?, ?, ?)""",
                     (cid, f["id"], t["id"], body.connection_type, body.direction,
                      int(bool(f["is_synthetic"] and t["is_synthetic"])), body.notes.strip(), now()))
        conn.execute("INSERT INTO activity (actor, actor_id, action, detail, created_at) VALUES (?,?,?,?,?)",
                     (user["name"], user["id"], "utility_connection_added",
                      f"{cid} recorded ({body.direction}), unverified: {body.notes.strip()}"[:300], now()))
    findings.safe_scan()
    return registry.detail(registry.get_row(f["asset_id"]))


class AssetStatusIn(BaseModel):
    operational_status: str = Field(pattern="^(in_service|out_of_service|under_repair|planned|abandoned)$")
    note: str = Field(min_length=5, max_length=300)


@router.post("/utility/assets/{asset_id}/status")
def set_asset_status(asset_id: str, body: AssetStatusIn, user: dict = Depends(require_admin)):
    a = _asset_or_404(asset_id)
    with db(immediate=True) as conn:
        conn.execute("UPDATE utility_assets SET operational_status=?, updated_at=? WHERE id=?",
                     (body.operational_status, now(), a["id"]))
        conn.execute("INSERT INTO activity (actor, actor_id, action, detail, created_at) VALUES (?,?,?,?,?)",
                     (user["name"], user["id"], "utility_asset_status",
                      f"{asset_id}: {a['operational_status'] or 'not recorded'} → {body.operational_status} — {body.note.strip()}"[:300], now()))
    return registry.detail(registry.get_row(asset_id))


class ContactIn(BaseModel):
    organization: str = Field(min_length=3, max_length=120)
    role: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=120)
    hours: str | None = Field(default=None, max_length=80)
    utility_type: str = Field(max_length=20)
    operator_org: str | None = Field(default=None, max_length=120)
    is_emergency: bool = False
    source_note: str = Field(min_length=5, max_length=200)


@router.post("/utility/contacts")
def add_contact(body: ContactIn, user: dict = Depends(require_admin)):
    """Register an operator contact. Stored UNVERIFIED until verified via /api/utility/records/verify."""
    if body.utility_type not in model.UTILITY_TYPES:
        raise HTTPException(422, "Unknown utility type")
    if not (body.phone or body.email):
        raise HTTPException(422, "Give a phone number or e-mail address")
    import secrets as _s
    cid = "CT-" + _s.token_hex(4).upper()
    with db(immediate=True) as conn:
        conn.execute("""INSERT INTO utility_contacts (contact_id, organization, role, phone, email, is_emergency, hours,
                        utility_type, operator_org, verification_status, visibility_level, is_synthetic, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?, 'unverified', 'officer', 0, ?)""",
                     (cid, body.organization.strip(), body.role.strip(), (body.phone or "").strip() or None,
                      (body.email or "").strip() or None, int(body.is_emergency), (body.hours or "").strip() or None,
                      body.utility_type, (body.operator_org or "").strip() or None, now()))
        conn.execute("INSERT INTO activity (actor, actor_id, action, detail, created_at) VALUES (?,?,?,?,?)",
                     (user["name"], user["id"], "utility_contact_added",
                      f"{cid} ({body.organization.strip()}) added unverified — source: {body.source_note.strip()}"[:300], now()))
    return {"contact_id": cid, "verification_status": "unverified"}


@router.get("/utility/contacts")
def list_contacts(user: dict = Depends(require_admin)):
    return query("""SELECT contact_id, organization, role, phone, email, utility_type, operator_org, is_emergency,
                    verification_status, last_verified_at, is_synthetic FROM utility_contacts ORDER BY utility_type, organization""")


class VerifyIn(BaseModel):
    kind: str = Field(pattern="^(asset|connection|contact)$")
    id: str = Field(min_length=1, max_length=64)
    status: str = Field(pattern="^(verified|unverified|disputed)$")
    note: str = Field(min_length=5, max_length=300)


@router.post("/utility/records/verify")
def verify_record(body: VerifyIn, user: dict = Depends(require_admin)):
    """Record a human verification decision (e.g. after a field survey or operator confirmation)."""
    table, col = {"asset": ("utility_assets", "asset_id"), "connection": ("utility_connections", "connection_id"),
                  "contact": ("utility_contacts", "contact_id")}[body.kind]
    stamp = "last_verified_at" if body.kind in ("asset", "contact") else None
    with db(immediate=True) as conn:
        sets = "verification_status=?" + (f", {stamp}=?" if stamp and body.status == "verified" else "")
        params = [body.status] + ([now()] if stamp and body.status == "verified" else []) + [body.id]
        cur = conn.execute(f"UPDATE {table} SET {sets} WHERE {col}=?", params)
        if cur.rowcount != 1:
            raise HTTPException(404, "Record not found")
        conn.execute("INSERT INTO activity (actor, actor_id, action, detail, created_at) VALUES (?,?,?,?,?)",
                     (user["name"], user["id"], "utility_record_verification",
                      f"{body.kind} {body.id} marked {body.status}: {body.note.strip()}"[:300], now()))
    findings.safe_scan()
    return {"ok": True, "kind": body.kind, "id": body.id, "status": body.status}
