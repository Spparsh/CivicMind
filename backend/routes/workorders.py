"""CivicMind — work orders and contractors.

Access policy
  GET  /api/contractors                                   officer, admin (contact details are staff-only)
  POST /api/admin/contractors                             admin
  POST /api/admin/contractors/{id}/status                 admin
  GET  /api/work-orders                                   officer, admin
  GET/POST /api/incidents/{incident_id}/work-orders       officer, admin
  GET  /api/work-orders/{id}                              officer, admin
  POST /api/work-orders/{id}/start|progress|evidence|submit|verify|cancel   officer, admin
  GET  /api/work-orders/{id}/evidence/{evidence_id}/photo officer, admin; a citizen with a linked report may
                                                          view AFTER photos of VERIFIED work only
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from auth import current_user, require_officer, require_admin, is_staff
from database import query, db, now
from routes.common import raise_from
from routes.incidents import _find
from services import evidence, workorders as svc

router = APIRouter(prefix="/api", tags=["work-orders"])
MAX_IMAGE_CHARS = 7_200_000


def _run(fn, *a, **k):
    try:
        return fn(*a, **k)
    except svc.WorkOrderError as e:
        raise_from(e)


def _incident(incident_id):
    inc = _find(incident_id)
    if not inc:
        raise HTTPException(404, "Incident not found")
    return inc


# --- contractors ----------------------------------------------------------------------
@router.get("/contractors")
def contractors(user: dict = Depends(require_officer)):
    return [svc.contractor_view(c) for c in query("SELECT * FROM contractors ORDER BY is_active DESC, name")]


class ContractorIn(BaseModel):
    contractor_code: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=3, max_length=120)
    department: str | None = Field(default=None, max_length=120)
    contact_person: str | None = Field(default=None, max_length=120)
    contact_role: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=120)
    office_address: str | None = Field(default=None, max_length=300)
    is_synthetic: bool = False


@router.post("/admin/contractors")
def create_contractor(body: ContractorIn, user: dict = Depends(require_admin)):
    return _run(svc.create_contractor, user, body.model_dump())


class ContractorStatusIn(BaseModel):
    is_active: bool | None = None
    verification_status: str | None = Field(default=None, pattern="^(verified|unverified|disputed)$")
    note: str = Field(min_length=5, max_length=300)


@router.post("/admin/contractors/{contractor_id}/status")
def contractor_status(contractor_id: int, body: ContractorStatusIn, user: dict = Depends(require_admin)):
    with db(immediate=True) as conn:
        c = conn.execute("SELECT * FROM contractors WHERE id=?", (contractor_id,)).fetchone()
        if not c:
            raise HTTPException(404, "Contractor not found")
        if body.is_active is not None:
            conn.execute("UPDATE contractors SET is_active=? WHERE id=?", (int(body.is_active), contractor_id))
        if body.verification_status:
            conn.execute("UPDATE contractors SET verification_status=? WHERE id=?", (body.verification_status, contractor_id))
        conn.execute("INSERT INTO activity (actor, actor_id, action, detail, created_at) VALUES (?,?,?,?,?)",
                     (user["name"], user["id"], "contractor_updated",
                      f"Contractor {c['contractor_code']} updated: {body.note.strip()}"[:300], now()))
    return svc.contractor_view(query("SELECT * FROM contractors WHERE id=?", (contractor_id,), one=True))


# --- work orders ----------------------------------------------------------------------
@router.get("/work-orders")
def list_work_orders(status: str | None = Query(None, pattern="^(open|assigned|in_progress|awaiting_verification|verified|cancelled|overdue)$"),
                     user: dict = Depends(require_officer)):
    sql, params = "SELECT * FROM work_orders", []
    if status == "open":
        sql += f" WHERE status IN {svc.OPEN}"
    elif status == "overdue":
        sql += " WHERE status IN ('assigned','in_progress') AND est_completion IS NOT NULL AND est_completion < ?"
        params.append(now())
    elif status:
        sql += " WHERE status=?"
        params.append(status)
    return [svc.view(w) | {"incident_public_id": (query("SELECT public_id FROM incidents WHERE id=?",
                                                        (w["incident_id"],), one=True) or {}).get("public_id")}
            for w in query(sql + " ORDER BY id DESC LIMIT 200", tuple(params))]


@router.get("/incidents/{incident_id}/work-orders")
def incident_work_orders(incident_id: str, user: dict = Depends(require_officer)):
    return svc.for_incident(_incident(incident_id)["id"])


class WorkOrderIn(BaseModel):
    task: str = Field(min_length=5, max_length=400)
    contractor_id: int | None = None
    team_name: str | None = Field(default=None, max_length=120)
    department: str | None = Field(default=None, max_length=120)
    priority: str = Field(default="medium", pattern="^(low|medium|high|critical)$")
    est_start: str | None = Field(default=None, max_length=25)
    est_completion: str | None = Field(default=None, max_length=25)
    utility_asset_ids: list[str] = Field(default_factory=list, max_length=10)


@router.post("/incidents/{incident_id}/work-orders")
def create_work_order(incident_id: str, body: WorkOrderIn, user: dict = Depends(require_officer)):
    return _run(svc.create, _incident(incident_id)["id"], user, body.model_dump())


@router.get("/work-orders/{wo_id}")
def get_work_order(wo_id: int, user: dict = Depends(require_officer)):
    wo = svc.get(wo_id)
    if not wo:
        raise HTTPException(404, "Work order not found")
    return wo


@router.post("/work-orders/{wo_id}/start")
def start(wo_id: int, user: dict = Depends(require_officer)):
    return _run(svc.start, wo_id, user)


class ProgressIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    est_completion: str | None = Field(default=None, max_length=25)


@router.post("/work-orders/{wo_id}/progress")
def progress(wo_id: int, body: ProgressIn, user: dict = Depends(require_officer)):
    return _run(svc.progress, wo_id, user, body.note, body.est_completion)


class EvidenceIn(BaseModel):
    kind: str = Field(pattern="^(before|after)$")
    photo: str = Field(max_length=MAX_IMAGE_CHARS)
    note: str | None = Field(default=None, max_length=300)


@router.post("/work-orders/{wo_id}/evidence")
def add_evidence(wo_id: int, body: EvidenceIn, user: dict = Depends(require_officer)):
    return _run(svc.add_evidence, wo_id, user, body.kind, body.photo, body.note)


class NoteIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.post("/work-orders/{wo_id}/submit")
def submit(wo_id: int, body: NoteIn = NoteIn(), user: dict = Depends(require_officer)):
    return _run(svc.submit, wo_id, user, body.note)


class VerifyIn(BaseModel):
    decision: str = Field(pattern="^(accept|reject)$")
    reason: str | None = Field(default=None, max_length=500)


@router.post("/work-orders/{wo_id}/verify")
def verify(wo_id: int, body: VerifyIn, user: dict = Depends(require_officer)):
    from services import actions as action_svc
    try:
        return svc.verify(wo_id, user, body.decision, body.reason)
    except svc.WorkOrderError as e:
        raise_from(e)
    except action_svc.WorkflowError as e:
        raise_from(e)


class CancelIn(BaseModel):
    reason: str = Field(min_length=5, max_length=300)


@router.post("/work-orders/{wo_id}/cancel")
def cancel(wo_id: int, body: CancelIn, user: dict = Depends(require_officer)):
    return _run(svc.cancel, wo_id, user, body.reason)


@router.get("/work-orders/{wo_id}/evidence/{evidence_id}/photo")
def evidence_photo(wo_id: int, evidence_id: int, user: dict = Depends(current_user)):
    row = query("""SELECT e.stored_name, e.kind, w.status, w.incident_id FROM work_order_evidence e
                   JOIN work_orders w ON w.id=e.work_order_id WHERE e.id=? AND e.work_order_id=?""",
                (evidence_id, wo_id), one=True)
    if not row:
        raise HTTPException(404, "Photo not available")
    if not is_staff(user):
        linked = query("SELECT 1 FROM complaints WHERE incident_id=? AND user_id=?", (row["incident_id"], user["id"]), one=True)
        if not (linked and row["kind"] == "after" and row["status"] == "verified"):
            raise HTTPException(404, "Photo not available")
    path = evidence.resolve_path(row["stored_name"])
    if not path:
        raise HTTPException(404, "Photo not available")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
