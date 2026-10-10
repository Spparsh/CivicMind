"""CivicMind — utility-operator coordination on an incident (officer / admin only).

  GET  /api/incidents/{incident_id}/operator-updates
  POST /api/incidents/{incident_id}/operator-updates
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import require_officer
from database import db
from routes.common import raise_from
from routes.incidents import _find
from services import coordination

router = APIRouter(prefix="/api", tags=["coordination"])


class OperatorUpdateIn(BaseModel):
    step: str = Field(max_length=40)
    operator_org: str | None = Field(default=None, max_length=120)
    contact_ref: str | None = Field(default=None, max_length=64)
    asset_ref: str | None = Field(default=None, max_length=64)
    reference: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)


def _incident(incident_id):
    inc = _find(incident_id)
    if not inc:
        raise HTTPException(404, "Incident not found")
    return inc


@router.get("/incidents/{incident_id}/operator-updates")
def list_updates(incident_id: str, user: dict = Depends(require_officer)):
    return coordination.list_updates(_incident(incident_id)["id"])


@router.post("/incidents/{incident_id}/operator-updates")
def add_update(incident_id: str, body: OperatorUpdateIn, user: dict = Depends(require_officer)):
    inc = _incident(incident_id)
    try:
        with db(immediate=True) as conn:
            fresh = conn.execute("SELECT id, status FROM incidents WHERE id=?", (inc["id"],)).fetchone()
            coordination.record(conn, dict(fresh), user, body.model_dump())
    except coordination.CoordinationError as e:
        raise_from(e)
    return coordination.list_updates(inc["id"])
