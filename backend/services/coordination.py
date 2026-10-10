"""
CivicMind — utility-operator coordination log for an incident.

Records what an officer did and what an authorized utility operator told
them (e.g. during a water-leak response). These are human-entered facts:

  * CivicMind never operates valves, breakers or gas controls, and never marks
    isolation as confirmed by itself — only an officer recording an
    operator's confirmation can.
  * Prerequisites are deterministic: an isolation outcome or operator
    guidance can only be recorded after the operator was contacted, and a
    crew dispatch only after isolation was confirmed or recorded as not
    required.
"""
from database import query, now

STEPS = {
    "operator_contacted": "Utility operator contacted",
    "operator_guidance_received": "Operator guidance received",
    "isolation_confirmed_by_operator": "Isolation confirmed by the operator",
    "isolation_not_required": "Operator: isolation not required",
    "crew_dispatched": "Repair crew dispatched",
}
NEEDS_CONTACT = {"operator_guidance_received", "isolation_confirmed_by_operator", "isolation_not_required"}
NEEDS_ISOLATION_OUTCOME = {"crew_dispatched"}
SAFETY_NOTE = ("CivicMind records coordination only. It does not operate valves, breakers or gas controls; "
               "the authorized utility operator identifies and operates the control point under approved "
               "procedures.")


class CoordinationError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, {}


def isolation_status(steps):
    present = set(steps)
    if "isolation_confirmed_by_operator" in present:
        return "confirmed_by_operator"
    if "isolation_not_required" in present:
        return "not_required"
    if "operator_contacted" in present:
        return "awaiting_operator"
    return "not_recorded"


def list_updates(incident_id, conn=None):
    sql = """SELECT o.id, o.step, o.operator_org, o.contact_ref, o.asset_ref, o.reference, o.note, o.created_at,
                    u.name AS recorded_by_name
             FROM incident_operator_updates o LEFT JOIN users u ON u.id=o.recorded_by
             WHERE o.incident_id=? ORDER BY o.created_at, o.id"""
    rows = [dict(r) for r in conn.execute(sql, (incident_id,)).fetchall()] if conn else query(sql, (incident_id,))
    for r in rows:
        r["step_label"] = STEPS.get(r["step"], r["step"])
    return {"updates": rows, "isolation_status": isolation_status([r["step"] for r in rows]),
            "steps": [{"key": k, "label": v} for k, v in STEPS.items()], "safety_note": SAFETY_NOTE}


def _clean(val, limit):
    val = " ".join(str(val or "").split())
    return val[:limit] or None


def record(conn, incident, actor, data):
    """Insert one coordination step inside the caller's (immediate) transaction."""
    step = data.get("step")
    if step not in STEPS:
        raise CoordinationError("Unknown coordination step.")
    if incident["status"] == "resolved":
        raise CoordinationError("This incident is already resolved.", 409)
    operator_org = _clean(data.get("operator_org"), 120)
    reference = _clean(data.get("reference"), 120)
    note = _clean(data.get("note"), 500)
    existing = {r["step"] for r in conn.execute(
        "SELECT step FROM incident_operator_updates WHERE incident_id=?", (incident["id"],)).fetchall()}
    if step in NEEDS_CONTACT and "operator_contacted" not in existing:
        raise CoordinationError("Record that the utility operator was contacted first.", 409)
    if step in NEEDS_ISOLATION_OUTCOME and not existing & {"isolation_confirmed_by_operator",
                                                           "isolation_not_required"}:
        raise CoordinationError("Record the operator's isolation decision before dispatching a crew.", 409)
    if step in ("operator_contacted", "isolation_confirmed_by_operator", "isolation_not_required") \
            and not operator_org:
        raise CoordinationError("Name the operator organisation.")
    if step == "isolation_confirmed_by_operator" and not (reference or note):
        raise CoordinationError("Add the operator's reference or a note describing the confirmation.")
    ts = now()
    cur = conn.execute(
        """INSERT INTO incident_operator_updates (incident_id, step, operator_org, contact_ref, asset_ref,
           reference, note, recorded_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (incident["id"], step, operator_org, _clean(data.get("contact_ref"), 64), _clean(data.get("asset_ref"), 64),
         reference, note, actor["id"], ts))
    detail = STEPS[step] + (f" ({operator_org})" if operator_org else "")
    conn.execute("""INSERT INTO activity (actor, actor_id, action, detail, incident_id, created_at)
                    VALUES (?,?,?,?,?,?)""", (actor["name"], actor["id"], "operator_" + step, detail,
                                               incident["id"], ts))
    return cur.lastrowid
