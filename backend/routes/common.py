"""CivicMind — shared route helpers: error mapping and purpose-specific views.

Never return raw database rows to clients. Each audience gets its own view:
  * owner_complaint  — the citizen who filed it
  * staff_complaint  — officers / admins (admins additionally see e-mail)
  * public tracking  — see routes/complaints.py::track (minimal subset)
"""
import json

from fastapi import HTTPException

from services import evidence
from services.complaints import next_step


def safe_json(val, default=None):
    if val is None:
        return default
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (TypeError, ValueError):
        return default


def raise_from(exc):
    """Convert a service-layer error into an HTTP error with a safe message."""
    detail = {"message": exc.message}
    detail.update(getattr(exc, "extra", {}) or {})
    raise HTTPException(status_code=exc.status, detail=detail)


def _photo_check(raw):
    return evidence.public_check(safe_json(raw))


def owner_complaint(c, incident=None, asset=None, officer_name=None):
    return {
        "id": c["id"], "public_id": c["public_id"], "tracking_code": c.get("tracking_code"),
        "category": c["category"], "description": c["description"], "address": c.get("address"),
        "ward": c.get("ward"), "zone": c.get("zone"),
        "latitude": c["latitude"], "longitude": c["longitude"],
        "status": c["status"], "severity": c["severity"],
        "classification": {"summary": c.get("ai_summary"), "engine": c.get("ai_engine")},
        "created_at": c["created_at"], "updated_at": c["updated_at"], "resolved_at": c.get("resolved_at"),
        "has_photo": bool(c.get("image_path")), "photo_check": _photo_check(c.get("proof_check")),
        "has_resolution_photo": bool(c.get("resolution_proof_path")),
        "resolution_check": _photo_check(c.get("resolution_proof_check")),
        "resolution_notes": c.get("resolution_notes"), "resolved_by_name": officer_name,
        "incident": incident, "asset": asset, "next_step": next_step(c["status"]),
        "is_synthetic": bool(c.get("is_synthetic")),
        "language": language_view(c),
    }


def language_view(c):
    """Original language plus a separately stored, citizen-reviewed English translation."""
    return {"original": c.get("original_language"), "detected": c.get("detected_language"),
            "translation_en": c.get("description_translation_en"),
            "translation_source": c.get("translation_engine"), "assistant_engine": c.get("assistant_engine")}


def staff_complaint(c, viewer, incident=None, asset=None, officer_name=None):
    out = owner_complaint(c, incident, asset, officer_name)
    out["incident_id"] = c.get("incident_id")
    out["citizen_name"] = c.get("citizen_name")
    out["classification"]["ai_category"] = c.get("ai_category")
    if viewer.get("role") == "admin":
        out["citizen_email"] = c.get("citizen_email")
    out.pop("tracking_code", None)  # the citizen's private code is not needed by staff
    return out
