"""CivicMind — incidents, investigation, plans and map data.

Access policy
  GET  /api/incidents                     officer, admin
  GET  /api/incidents/{id}                officer/admin -> full workspace view
                                          citizen with a linked report -> limited view
                                          anyone else -> 404
  POST /api/incidents/detect              officer, admin
  POST /api/incidents/{id}/investigate    officer, admin
  POST /api/incidents/{id}/plans          officer, admin  (new plan; only if none open)
  GET  /api/map/complaints                officer/admin -> all; citizen -> own only
  GET  /api/map/incidents                 officer/admin -> full; citizen -> generalised, no members
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import current_user, require_officer, is_staff
from database import query, execute, db, now
from services import clustering, ai, geo, risk, evidence, assets as asset_svc
from services import actions as action_svc, coordination, voice as voice_svc, media as media_svc
from routes.common import safe_json, raise_from, language_view

router = APIRouter(prefix="/api", tags=["incidents"])
SORTS = {"priority": "priority_score DESC", "recent": "created_at DESC", "reports": "report_count DESC"}


def _find(incident_id: str):
    inc = None
    if incident_id.isdigit():
        inc = query("SELECT * FROM incidents WHERE id=?", (int(incident_id),), one=True)
    if not inc and len(incident_id) <= 20:
        inc = query("SELECT * FROM incidents WHERE public_id=?", (incident_id,), one=True)
    return inc


def _priority(inc):
    p = safe_json(inc.get("priority_factors"), {}) or {}
    return {"score": inc["priority_score"], "label": inc["priority_label"],
            "factors": p.get("factors", []), "reasons": p.get("reasons", []),
            "model": p.get("model"), "text": inc.get("priority_reason")}


def _action_view(a, staff):
    out = {k: a.get(k) for k in ("id", "status", "action_text", "assigned_team", "department",
                                 "eta_hours", "created_at", "approved_at", "started_at",
                                 "completed_at", "source")}
    out["checklist"] = safe_json(a.get("checklist"), []) or []
    out["has_proof"] = bool(a.get("proof_path"))
    out["proof_check"] = evidence.public_check(safe_json(a.get("proof_check")))
    if staff:
        out.update({k: a.get(k) for k in ("notes", "rejection_reason", "rejected_at",
                                          "approved_by_name", "rejected_by_name", "completed_by_name")})
    return out


@router.get("/incidents")
def list_incidents(status: str | None = Query(None, max_length=20),
                   priority: str | None = Query(None, max_length=10),
                   category: str | None = Query(None, max_length=60),
                   ward: str | None = Query(None, max_length=60),
                   q: str | None = Query(None, max_length=80), sort: str = "priority",
                   limit: int = Query(200, ge=1, le=500),
                   user: dict = Depends(require_officer)):
    sql, params = "SELECT * FROM incidents WHERE 1=1", []
    if status == "open":
        sql += " AND status != 'resolved'"
    elif status:
        sql += " AND status=?"
        params.append(status)
    for col, val in (("priority_label", priority), ("category", category), ("ward", ward)):
        if val:
            sql += f" AND {col}=?"
            params.append(val)
    if q:
        sql += " AND (title LIKE ? OR public_id LIKE ? OR ward LIKE ?)"
        params += [f"%{q}%"] * 3
    sql += f" ORDER BY {SORTS.get(sort, SORTS['priority'])} LIMIT ?"
    params.append(limit)
    rows = query(sql, tuple(params))
    for r in rows:
        r["priority"] = _priority(r)
        for k in ("investigation", "priority_factors", "grouping_basis", "confidence", "affected_people"):
            r.pop(k, None)
    return rows


@router.get("/incidents/{incident_id}")
def incident_detail(incident_id: str, user: dict = Depends(current_user)):
    inc = _find(incident_id)
    if not inc:
        raise HTTPException(404, "Incident not found")
    staff = is_staff(user)
    if not staff:
        mine = query("SELECT id FROM complaints WHERE incident_id=? AND user_id=?", (inc["id"], user["id"]))
        if not mine:
            raise HTTPException(404, "Incident not found")
        return _citizen_view(inc, user)
    return _staff_view(inc)


def _actions(inc_id):
    return query("""SELECT a.*, ua.name AS approved_by_name, ur.name AS rejected_by_name,
                           uc.name AS completed_by_name
                    FROM actions a LEFT JOIN users ua ON ua.id=a.approved_by
                    LEFT JOIN users ur ON ur.id=a.rejected_by LEFT JOIN users uc ON uc.id=a.completed_by
                    WHERE a.incident_id=? ORDER BY a.id DESC""", (inc_id,))


def _resolution(inc_id):
    r = query("""SELECT r.*, u.name AS officer_name FROM resolutions r LEFT JOIN users u ON u.id=r.resolved_by
                 WHERE r.incident_id=? ORDER BY r.id DESC LIMIT 1""", (inc_id,), one=True)
    if not r:
        return None
    return {k: r.get(k) for k in ("id", "action_id", "status", "summary", "verification_type",
                                  "before_metric", "after_metric", "verified_at", "officer_name")} | {
        "proof_check": evidence.public_check(safe_json(r.get("proof_check"))),
        "has_proof": bool(r.get("proof_path"))}


def _staff_view(inc):
    out = {k: inc.get(k) for k in ("id", "public_id", "title", "category", "severity", "status", "ward",
                                   "zone", "centroid_lat", "centroid_lng", "report_count", "spread_km",
                                   "duration_hours", "summary", "probable_cause", "created_at",
                                   "updated_at", "resolved_at", "last_report_at")}
    out["distinct_reporters"] = inc.get("affected_people")
    out["investigation"] = safe_json(inc.get("investigation"), {})
    out["priority"] = _priority(inc)
    out["grouping_basis"] = safe_json(inc.get("grouping_basis"))
    out["area_method"] = geo.WARD_METHOD
    out["complaints"] = []
    for c in query("""SELECT c.*, u.name AS citizen_name FROM complaints c
                      JOIN incident_complaints ic ON ic.complaint_id=c.id LEFT JOIN users u ON u.id=c.user_id
                      WHERE ic.incident_id=? ORDER BY c.created_at""", (inc["id"],)):
        m = query("""SELECT a.asset_code, ca.distance_m, ca.status FROM complaint_assets ca
                     JOIN assets a ON a.id=ca.asset_id WHERE ca.complaint_id=?""", (c["id"],), one=True)
        out["complaints"].append({
            "id": c["id"], "public_id": c["public_id"], "category": c["category"],
            "description": c["description"], "status": c["status"], "severity": c["severity"],
            "latitude": c["latitude"], "longitude": c["longitude"], "created_at": c["created_at"],
            "citizen_name": c["citizen_name"], "has_photo": bool(c["image_path"]),
            "photo_check": evidence.public_check(safe_json(c["proof_check"])),
            "asset_match": m, "is_synthetic": bool(c.get("is_synthetic")),
            "language": language_view(c),
            "voice": voice_svc.for_complaint(c["id"]),
            "media": media_svc.for_complaint(c["id"]),
        })
    out["evidence"] = query("SELECT evidence_type, value, source FROM evidence WHERE incident_id=? ORDER BY id",
                            (inc["id"],))
    acts = _actions(inc["id"])
    out["actions"] = [_action_view(a, True) for a in acts]
    open_ = [a for a in out["actions"] if a["status"] in action_svc.OPEN_ACTION]
    out["current_action"] = open_[0] if open_ else (out["actions"][0] if out["actions"] else None)
    out["can_generate_plan"] = not open_ and inc["status"] != "resolved"
    out["resolution"] = _resolution(inc["id"])
    a = asset_svc.incident_asset(inc["id"])
    if a:
        out["asset"] = asset_svc.asset_public(a) | {"matched_reports": a["matched_reports"],
                                                    "min_distance_m": a["min_distance_m"]}
        out["asset"]["recent_maintenance"] = query(
            """SELECT event_type, performed_at, outcome, data_source, is_synthetic FROM asset_maintenance
               WHERE asset_id=? ORDER BY performed_at DESC LIMIT 5""", (a["id"],))
    else:
        out["asset"] = None
    out["findings"] = [risk.finding_out(f) for f in query(
        """SELECT * FROM risk_findings WHERE status IN ('open','acknowledged')
           AND (incident_id=? OR (asset_id IS NOT NULL AND asset_id=?))
           ORDER BY detected_at DESC""", (inc["id"], a["id"] if a else -1))]
    out["timeline"] = query("""SELECT actor, action, detail, created_at FROM activity WHERE incident_id=?
                               ORDER BY created_at DESC, id DESC LIMIT 50""", (inc["id"],))
    out["coordination"] = coordination.list_updates(inc["id"])
    out["view"] = "staff"
    return out


CITIZEN_TIMELINE = {
    "incident_detected": "Reports grouped into this incident",
    "report_linked": "Another report was linked",
    "action_approved": "Response plan approved",
    "action_started": "Work started",
    "action_completed": "Work completed",
    "resolution_verified": "Marked resolved",
    "incident_reopened": "Incident reopened",
    "incident_closed": "Marked resolved",
    "operator_crew_dispatched": "Repair crew dispatched",
}


def _citizen_view(inc, user):
    lat, lng = geo.generalise(inc["centroid_lat"], inc["centroid_lng"])
    mine = query("""SELECT id, public_id, status, created_at FROM complaints
                    WHERE incident_id=? AND user_id=? ORDER BY created_at""", (inc["id"], user["id"]))
    acts = _actions(inc["id"])
    current = next((a for a in acts if a["status"] in action_svc.OPEN_ACTION + ("completed",)), None)
    timeline = [{"action": CITIZEN_TIMELINE[t["action"]], "created_at": t["created_at"]}
                for t in query("SELECT action, created_at FROM activity WHERE incident_id=? "
                               "ORDER BY created_at DESC, id DESC LIMIT 30", (inc["id"],))
                if t["action"] in CITIZEN_TIMELINE]
    res = _resolution(inc["id"])
    return {
        "view": "citizen",
        "id": inc["id"], "public_id": inc["public_id"], "title": inc["title"], "category": inc["category"],
        "status": inc["status"], "ward": inc["ward"], "report_count": inc["report_count"],
        "centroid_lat": lat, "centroid_lng": lng, "created_at": inc["created_at"],
        "priority": {"label": inc["priority_label"]},
        "my_reports": mine,
        "current_action": {"status": current["status"], "action_text": current["action_text"],
                           "department": current["department"], "eta_hours": current["eta_hours"]}
        if current and current["status"] != "pending" else None,
        "resolution": {"status": res["status"], "verified_at": res["verified_at"],
                       "officer_name": res["officer_name"], "verification_type": res["verification_type"]}
        if res else None,
        "timeline": timeline,
    }


@router.post("/incidents/detect")
def detect(user: dict = Depends(require_officer)):
    created = clustering.detect_incidents()
    execute("INSERT INTO activity (actor, actor_id, action, detail) VALUES (?,?,?,?)",
            (user["name"], user["id"], "detection_run", f"Grouping run: {len(created)} new incident(s)"))
    risk.safe_scan()
    return {"detected": len(created), "incidents": [{"id": i["id"], "public_id": i["public_id"],
                                                     "title": i["title"]} for i in created]}


@router.post("/incidents/{incident_id}/investigate")
def investigate(incident_id: int, user: dict = Depends(require_officer)):
    inc = query("SELECT * FROM incidents WHERE id=?", (incident_id,), one=True)
    if not inc:
        raise HTTPException(404, "Incident not found")
    cats = sorted({m["category"] for m in query(
        "SELECT category FROM complaints WHERE incident_id=?", (incident_id,))})
    asset = asset_svc.incident_asset(incident_id)
    facts = {"category": inc["category"], "categories": cats, "report_count": inc["report_count"],
             "spread_km": inc["spread_km"], "duration_hours": inc["duration_hours"],
             "ward": inc["ward"], "severity": inc["severity"],
             "asset": f"{asset_svc.type_label(asset['asset_type'])} {asset['asset_code']}" if asset else None}
    inv = ai.investigate_incident(facts)
    with db() as conn:
        conn.execute("UPDATE incidents SET investigation=?, summary=?, probable_cause=?, updated_at=? WHERE id=?",
                     (json.dumps(inv), inv.get("summary"), inv.get("probable_cause"), now(), incident_id))
        conn.execute("INSERT INTO activity (actor, actor_id, action, detail, incident_id) VALUES (?,?,?,?,?)",
                     (user["name"], user["id"], "investigation_run",
                      f"Investigation notes regenerated ({'AI-assisted' if inv.get('engine') == 'sarvam' else 'rule-based'})",
                      incident_id))
    inv.pop("ai_error", None)
    return inv


@router.post("/incidents/{incident_id}/plans")
@router.post("/incidents/{incident_id}/action", include_in_schema=False)  # legacy path
def generate_plan(incident_id: int, user: dict = Depends(require_officer)):
    try:
        return _action_view(action_svc.generate_plan(incident_id, user), True)
    except action_svc.WorkflowError as e:
        raise_from(e)


# --- map data --------------------------------------------------------------
@router.get("/map/complaints")
def map_complaints(user: dict = Depends(current_user)):
    sql = """SELECT id, public_id, category, severity, status, latitude, longitude, ward, incident_id,
                    created_at, is_synthetic FROM complaints WHERE status != 'rejected'"""
    if not is_staff(user):
        return query(sql + " AND user_id=? ORDER BY created_at DESC LIMIT 200", (user["id"],))
    return query(sql + " ORDER BY created_at DESC LIMIT 2000")


@router.get("/map/incidents")
def map_incidents(user: dict = Depends(current_user)):
    rows = query("""SELECT id, public_id, title, category, severity, priority_score, priority_label,
                    status, ward, zone, centroid_lat, centroid_lng, spread_km, report_count, asset_id
                    FROM incidents""")
    if is_staff(user):
        for r in rows:
            r["members"] = query("""SELECT latitude, longitude FROM complaints WHERE incident_id=?""", (r["id"],))
        return rows
    linked = {r["incident_id"] for r in query(
        "SELECT DISTINCT incident_id FROM complaints WHERE user_id=? AND incident_id IS NOT NULL", (user["id"],))}
    out = []
    for r in rows:
        lat, lng = geo.generalise(r["centroid_lat"], r["centroid_lng"])
        out.append({"id": r["id"] if r["id"] in linked else None, "linked": r["id"] in linked,
                    "title": r["title"], "category": r["category"], "status": r["status"],
                    "priority_label": r["priority_label"], "ward": r["ward"], "report_count": r["report_count"],
                    "centroid_lat": lat, "centroid_lng": lng})
    return out


@router.get("/wards")
def wards():
    return [{"ward": w, "lat": v[0], "lng": v[1], "zone": v[2]} for w, v in geo.WARDS.items()]
