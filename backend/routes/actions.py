"""CivicMind — action lifecycle routes (officer / admin only).

State rules live in services/actions.py; these routes only authenticate,
validate the request shape and translate errors.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from auth import current_user, require_officer, is_staff
from database import query
from services import actions as svc, evidence
from routes.common import raise_from
from routes.incidents import _action_view

router = APIRouter(prefix="/api", tags=["actions"])
MAX_IMAGE_CHARS = 7_200_000


class ApproveIn(BaseModel):
    action_text: str | None = Field(default=None, max_length=400)
    assigned_team: str | None = Field(default=None, max_length=120)
    department: str | None = Field(default=None, max_length=120)
    eta_hours: float | None = None
    checklist: list[str] | None = Field(default=None, max_length=15)


class RejectIn(BaseModel):
    reason: str = Field(default="", max_length=300)


class CompleteIn(BaseModel):
    notes: str | None = Field(default=None, max_length=500)
    proof: str | None = Field(default=None, max_length=MAX_IMAGE_CHARS)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except svc.WorkflowError as e:
        raise_from(e)


@router.post("/actions/{action_id}/approve")
def approve(action_id: int, body: ApproveIn = ApproveIn(), user: dict = Depends(require_officer)):
    return _action_view(_run(svc.approve_action, action_id, user, body.model_dump(exclude_none=True)), True)


@router.post("/actions/{action_id}/reject")
def reject(action_id: int, body: RejectIn = RejectIn(), user: dict = Depends(require_officer)):
    return _action_view(_run(svc.reject_action, action_id, user, body.reason), True)


@router.post("/actions/{action_id}/start")
def start(action_id: int, user: dict = Depends(require_officer)):
    return _action_view(_run(svc.start_action, action_id, user), True)


@router.post("/actions/{action_id}/complete")
def complete(action_id: int, body: CompleteIn = CompleteIn(), user: dict = Depends(require_officer)):
    """Complete the work. A proof photo is mandatory and must pass the automated checks."""
    res = _run(svc.complete_action, action_id, user, body.notes or "", body.proof)
    return {"ok": True, "incident_status": "resolved" if res["status"] == "verified" else "reopened",
            "resolution": {k: res.get(k) for k in ("id", "status", "summary", "verification_type", "verified_at")}}


@router.post("/incidents/{incident_id}/verify")
def verify(incident_id: int, user: dict = Depends(require_officer)):
    res = _run(svc.verify_incident, incident_id, user)
    return {k: res.get(k) for k in ("id", "status", "summary", "verification_type", "verified_at",
                                    "before_metric", "after_metric")}


@router.get("/actions/{action_id}/proof")
def action_proof(action_id: int, user: dict = Depends(current_user)):
    a = query("SELECT incident_id, proof_path FROM actions WHERE id=?", (action_id,), one=True)
    if not a:
        raise HTTPException(404, "Photo not available")
    if not is_staff(user) and not query("SELECT 1 FROM complaints WHERE incident_id=? AND user_id=?",
                                        (a["incident_id"], user["id"]), one=True):
        raise HTTPException(404, "Photo not available")
    path = evidence.resolve_path(a["proof_path"])
    if not path:
        raise HTTPException(404, "Photo not available")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
