"""CivicMind — complaint submission, listing, evidence, tracking and notifications.

Access policy
  POST /api/complaints                    citizen
  GET  /api/complaints                    citizen -> own only;  officer/admin -> all
  GET  /api/complaints/{id}               owner, officer, admin  (others: 404)
  GET  /api/complaints/{id}/photo         owner, officer, admin
  GET  /api/complaints/{id}/resolution-photo  owner, officer, admin
  GET  /api/complaints/{id}/plan          officer, admin
  POST /api/complaints/{id}/resolve       officer, admin (proof photo required)
  GET  /api/track/{tracking_code}         public, minimal fields only
  GET/POST /api/notifications[...]        own notifications only
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from auth import current_user, require_citizen, require_officer, is_staff
from database import query, execute
from services import ai, evidence, complaints as intake, assets as asset_svc
from services import actions as actions_svc, voice as voice_svc, workorders as wo_svc, media as media_svc
from routes.common import owner_complaint, staff_complaint, raise_from, safe_json
from config import MEDIA_MAX_FILES

router = APIRouter(prefix="/api", tags=["complaints"])
MAX_IMAGE_CHARS = 7_200_000  # ~5 MB of base64


class ComplaintIn(BaseModel):
    category: str = Field(max_length=60)
    description: str = Field(max_length=2000)
    latitude: float
    longitude: float
    address: str | None = Field(default=None, max_length=300)
    image: str | None = Field(default=None, max_length=MAX_IMAGE_CHARS)
    # Optional multilingual metadata. `description` is always the citizen's own words.
    language: str | None = Field(default=None, max_length=5)
    translation_en: str | None = Field(default=None, max_length=2000)
    assistant_engine: str | None = Field(default=None, max_length=20)
    voice_id: str | None = Field(default=None, max_length=64)
    location_source: str | None = Field(default=None, max_length=20)
    location_accuracy_m: float | None = None
    geocoded_address: str | None = Field(default=None, max_length=300)
    media_ids: list[str] | None = Field(default=None, max_length=MEDIA_MAX_FILES)


class ResolveIn(BaseModel):
    notes: str | None = Field(default=None, max_length=500)
    proof: str | None = Field(default=None, max_length=MAX_IMAGE_CHARS)
    plan: dict | None = None
    eta_hours: float | None = None


def _load_for(complaint_id, user):
    c = query("""SELECT c.*, u.name AS citizen_name, u.email AS citizen_email, o.name AS officer_name
                 FROM complaints c LEFT JOIN users u ON u.id=c.user_id
                 LEFT JOIN users o ON o.id=c.resolved_by WHERE c.id=?""", (complaint_id,), one=True)
    if not c or not (is_staff(user) or c["user_id"] == user["id"]):
        raise HTTPException(404, "Complaint not found")
    return c


def _extras(c):
    inc = None
    if c.get("incident_id"):
        i = query("SELECT id, public_id, title, status FROM incidents WHERE id=?", (c["incident_id"],), one=True)
        inc = i
    m = query("""SELECT a.asset_code, a.asset_type, ca.distance_m, ca.status FROM complaint_assets ca
                 JOIN assets a ON a.id=ca.asset_id WHERE ca.complaint_id=? AND ca.status != 'rejected'""",
              (c["id"],), one=True)
    asset = None
    if m:
        asset = {"asset_code": m["asset_code"], "type_label": asset_svc.type_label(m["asset_type"]),
                 "distance_m": m["distance_m"], "match_status": m["status"]}
    return inc, asset


@router.post("/complaints")
def create_complaint(body: ComplaintIn, user: dict = Depends(require_citizen)):
    try:
        return intake.create(user, body.category, body.description, body.latitude, body.longitude,
                             body.address, body.image, language=body.language,
                             translation_en=body.translation_en, assistant_engine=body.assistant_engine,
                             voice_id=body.voice_id, location_source=body.location_source,
                             location_accuracy_m=body.location_accuracy_m, geocoded_address=body.geocoded_address,
                             media_ids=[m[:64] for m in (body.media_ids or [])])
    except intake.IntakeError as e:
        raise_from(e)


@router.get("/complaints")
def list_complaints(
    status: str | None = Query(None, max_length=20), category: str | None = Query(None, max_length=60),
    ward: str | None = Query(None, max_length=60), q: str | None = Query(None, max_length=80),
    limit: int = Query(200, ge=1, le=500), user: dict = Depends(current_user),
):
    sql = """SELECT c.*, u.name AS citizen_name, u.email AS citizen_email, o.name AS officer_name
             FROM complaints c LEFT JOIN users u ON u.id=c.user_id
             LEFT JOIN users o ON o.id=c.resolved_by WHERE 1=1"""
    params = []
    if not is_staff(user):
        sql += " AND c.user_id=?"
        params.append(user["id"])
    for col, val in (("c.status", status), ("c.category", category), ("c.ward", ward)):
        if val:
            sql += f" AND {col}=?"
            params.append(val)
    if q:
        sql += " AND (c.description LIKE ? OR c.public_id LIKE ? OR c.address LIKE ?)"
        params += [f"%{q}%"] * 3
    sql += " ORDER BY c.created_at DESC, c.id DESC LIMIT ?"
    params.append(limit)
    out = []
    for c in query(sql, tuple(params)):
        inc, asset = _extras(c)
        item = (staff_complaint(c, user, inc, asset, c.get("officer_name")) if is_staff(user)
                else owner_complaint(c, inc, asset, c.get("officer_name")))
        if not is_staff(user):  # the citizen's own list shows progress at a glance
            item |= _tracking(c, False)
        out.append(item)
    return out


@router.get("/complaints/{complaint_id}")
def get_complaint(complaint_id: int, user: dict = Depends(current_user)):
    c = _load_for(complaint_id, user)
    inc, asset = _extras(c)
    if is_staff(user):
        out = staff_complaint(c, user, inc, asset, c.get("officer_name"))
        out["resolution_plan"] = safe_json(c.get("resolution_plan"))
    else:
        out = owner_complaint(c, inc, asset, c.get("officer_name"))
    return out | _tracking(c, is_staff(user))


# Citizen-safe labels for activity entries (internal notes are never shown to citizens).
HISTORY_LABELS = {
    "complaint_submitted": "Report submitted", "incident_detected": "Grouped with similar reports into an incident",
    "report_linked": "Linked to an existing incident", "action_approved": "Response plan approved",
    "action_started": "Work started", "action_completed": "Work completed with a proof photo",
    "resolution_verified": "Resolved after workflow verification", "incident_reopened": "Reopened — the problem was reported again",
    "complaint_resolved": "Resolved by an officer", "incident_closed": "Resolved",
    "operator_crew_dispatched": "Repair crew dispatched",
    "wo_created": "Work order issued", "wo_started": "Work started on site", "wo_estimate_changed": "Estimated completion updated",
    "wo_submitted": "Work reported complete — awaiting verification", "wo_rejected": "Completion not accepted yet — work continues",
    "wo_accepted": "Completed work verified by an officer",
}


def _tracking(c, staff):
    """Status history, completion estimate, voice transcript and resolution photos for one complaint."""
    rows = query("""SELECT action, detail, created_at, actor FROM activity WHERE complaint_id=?
                    OR (incident_id IS NOT NULL AND incident_id=?) ORDER BY created_at, id""",
                 (c["id"], c.get("incident_id") or -1))
    history = [{"event": r["action"], "label": HISTORY_LABELS[r["action"]], "at": r["created_at"],
                **({"detail": r["detail"], "actor": r["actor"]} if staff else {})}
               for r in rows if r["action"] in HISTORY_LABELS or staff]
    for h in history:
        h.setdefault("label", h["event"].replace("_", " "))
    inc = query("SELECT * FROM incidents WHERE id=?", (c["incident_id"],), one=True) if c.get("incident_id") else None
    if c["status"] == "resolved" and c.get("resolved_at") and not inc:
        estimate = {"kind": "actual", "at": c["resolved_at"], "basis": "Recorded resolution time."}
    else:
        estimate = wo_svc.estimate_for_incident(inc)
    photos = []
    if c.get("resolution_proof_path"):
        photos.append({"url": f"/api/complaints/{c['id']}/resolution-photo", "label": "Resolution photo"})
    if inc:
        for a in query("""SELECT id FROM actions WHERE incident_id=? AND status='completed' AND proof_path IS NOT NULL
                          ORDER BY id DESC""", (inc["id"],)):
            photos.append({"url": f"/api/actions/{a['id']}/proof", "label": "Completion photo"})
    wo_status = None
    if inc:
        wo = query("""SELECT status, department FROM work_orders WHERE incident_id=? AND status != 'cancelled'
                      ORDER BY id DESC LIMIT 1""", (inc["id"],), one=True)
        if wo:
            wo_status = {"status": wo["status"], "department": wo["department"]}
    return {"history": history, "estimate": estimate, "voice": voice_svc.for_complaint(c["id"]),
            "media": media_svc.for_complaint(c["id"]),
            "resolution_photos": photos, "work_order": wo_status,
            "location": {"source": c.get("location_source"), "accuracy_m": c.get("location_accuracy_m"),
                         "geocoded_address": c.get("geocoded_address")}}


def _file(stored):
    path = evidence.resolve_path(stored)
    if not path:
        raise HTTPException(404, "Photo not available")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=300",
                                 "Content-Disposition": "inline"})


@router.get("/complaints/{complaint_id}/photo")
def complaint_photo(complaint_id: int, user: dict = Depends(current_user)):
    return _file(_load_for(complaint_id, user).get("image_path"))


@router.get("/complaints/{complaint_id}/resolution-photo")
def complaint_resolution_photo(complaint_id: int, user: dict = Depends(current_user)):
    return _file(_load_for(complaint_id, user).get("resolution_proof_path"))


@router.get("/complaints/{complaint_id}/plan")
def complaint_plan(complaint_id: int, user: dict = Depends(require_officer)):
    """Draft resolution plan for a single complaint (a recommendation only)."""
    c = _load_for(complaint_id, user)
    _, asset = _extras(c)
    plan = ai.recommend_action({"category": c["category"], "ward": c["ward"], "severity": c["severity"],
                                "report_count": 1,
                                "asset": f"{asset['type_label']} {asset['asset_code']}" if asset else None})
    plan["eta_label"] = _eta_label(plan.get("eta_hours", 24))
    plan["is_recommendation"] = True
    plan.pop("ai_error", None)
    return plan


@router.post("/complaints/{complaint_id}/resolve")
def resolve_complaint(complaint_id: int, body: ResolveIn, user: dict = Depends(require_officer)):
    try:
        return actions_svc.resolve_complaint(complaint_id, user, body.notes or "", body.proof,
                                             plan=body.plan, eta_hours=body.eta_hours)
    except actions_svc.WorkflowError as e:
        raise_from(e)


def _eta_label(hours):
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return "—"
    if h < 24:
        return f"{h:g} hours"
    d = round(h / 24, 1)
    return f"{d:g} day" + ("s" if d != 1 else "")


@router.get("/track/{code}")
def track(code: str):
    """Public status lookup by the PRIVATE tracking code given to the citizen.

    Deliberately minimal: no description, no exact location, no names, no
    photos, no internal notes. Sequential CMP- ids are not accepted here, so
    the endpoint cannot be enumerated."""
    code = (code or "").strip().upper()
    if not (8 <= len(code) <= 16 and code.isalnum()):
        raise HTTPException(404, "No complaint found for that tracking code.")
    c = query("SELECT * FROM complaints WHERE tracking_code=?", (code,), one=True)
    if not c:
        raise HTTPException(404, "No complaint found for that tracking code.")
    incident = None
    if c["incident_id"]:
        i = query("SELECT public_id, status, report_count FROM incidents WHERE id=?", (c["incident_id"],), one=True)
        incident = {"public_id": i["public_id"], "status": i["status"], "report_count": i["report_count"]} if i else None
    return {
        "public_id": c["public_id"], "category": c["category"], "status": c["status"],
        "ward": c["ward"], "submitted_on": (c["created_at"] or "")[:10],
        "last_update": c["updated_at"], "incident": incident,
        "next_step": intake.next_step(c["status"]),
    }


# --- notifications ---------------------------------------------------------
@router.get("/notifications")
def notifications(unread: int = 0, user: dict = Depends(current_user)):
    sql = """SELECT id, complaint_id, incident_id, title, message, type, is_read, created_at
             FROM notifications WHERE user_id=?"""
    if unread:
        sql += " AND is_read=0"
    rows = query(sql + " ORDER BY created_at DESC, id DESC LIMIT 100", (user["id"],))
    unread_count = query("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0",
                         (user["id"],), one=True)["c"]
    return {"items": rows, "unread": unread_count}


@router.post("/notifications/read")
def mark_read(user: dict = Depends(current_user)):
    execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
    return {"ok": True}
