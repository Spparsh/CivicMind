"""
CivicMind — response plans, execution and resolution (explicit state machine).

Action states:   pending -> approved -> in_progress -> completed
                 pending -> rejected
                 pending|approved|in_progress -> cancelled   (incident closed another way)
Incident states: active -> in_progress -> resolved -> reopened -> in_progress -> ...

Rules enforced here (the UI is never trusted to enforce them):
  * at most ONE open plan (pending/approved/in_progress) per incident;
  * every transition checks the CURRENT state inside a write-locked
    transaction and fails with 409 if it changed meanwhile;
  * completion requires a proof photo that passes the automated checks;
  * verification requires a completed action with an accepted proof photo
    that still exists on disk — it can never resolve an incident without proof;
  * the acting user's id and name are recorded on every step.

"Verification" here means WORKFLOW verification (completed plan + accepted
proof photo + no new related reports since completion). It is not an
independent physical inspection, and results say so.
"""
import json

from config import CLUSTER_RADIUS_KM
from database import query, db, now
from services import ai, evidence, clustering, risk, assets as asset_svc, geo

OPEN_ACTION = ("pending", "approved", "in_progress")
VERIFY_NOTE = ("Workflow verification: completed plan, proof photo passed automated checks, "
               "and recurrence check. Not an independent field inspection.")


class WorkflowError(Exception):
    def __init__(self, message, status=409, extra=None):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra or {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _log(conn, actor, action, detail, incident_id=None, complaint_id=None):
    conn.execute("""INSERT INTO activity (actor, actor_id, action, detail, incident_id, complaint_id, created_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (actor["name"] if actor else "CivicMind", actor["id"] if actor else None,
                  action, detail, incident_id, complaint_id, now()))


def _notify_incident(conn, incident_id, title, message, ntype):
    for r in conn.execute("SELECT user_id, id FROM complaints WHERE incident_id=?", (incident_id,)).fetchall():
        conn.execute("""INSERT INTO notifications (user_id, complaint_id, incident_id, title, message, type, created_at)
                        VALUES (?,?,?,?,?,?,?)""",
                     (r["user_id"], r["id"], incident_id, title, message, ntype, now()))


def _load_action(conn, action_id):
    a = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    if not a:
        raise WorkflowError("Action not found", 404)
    inc = conn.execute("SELECT * FROM incidents WHERE id=?", (a["incident_id"],)).fetchone()
    return dict(a), dict(inc)


def _require(action, *states):
    if action["status"] not in states:
        raise WorkflowError(
            f"This plan is '{action['status']}'; this step needs it to be {' or '.join(states)}. "
            "Refresh to see the current state.")


def _transition(conn, table, row_id, expected, fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    cur = conn.execute(f"UPDATE {table} SET {cols} WHERE id=? AND status=?",
                       (*fields.values(), row_id, expected))
    if cur.rowcount != 1:
        raise WorkflowError("The record changed while you were working. Refresh and try again.")


def _clean_text(val, field, lo, hi):
    val = (val or "").strip()
    if not lo <= len(val) <= hi:
        raise WorkflowError(f"{field} must be {lo}-{hi} characters.", 422)
    return val


def validate_edits(edits):
    out = {}
    if not edits:
        return out
    if edits.get("action_text") is not None:
        out["action_text"] = _clean_text(edits["action_text"], "Action", 5, 400)
    for k, label in (("assigned_team", "Team"), ("department", "Department")):
        if edits.get(k) is not None:
            out[k] = _clean_text(edits[k], label, 2, 120)
    if edits.get("eta_hours") is not None:
        try:
            eta = float(edits["eta_hours"])
        except (TypeError, ValueError):
            raise WorkflowError("ETA must be a number of hours.", 422)
        if not 1 <= eta <= 720:
            raise WorkflowError("ETA must be between 1 and 720 hours.", 422)
        out["eta_hours"] = eta
    if edits.get("checklist") is not None:
        items = [str(x).strip()[:200] for x in edits["checklist"] if str(x).strip()]
        if len(items) > 15:
            raise WorkflowError("Checklist can have at most 15 items.", 422)
        out["checklist"] = json.dumps(items)
    return out


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
def generate_plan(incident_id, actor):
    inc = query("SELECT * FROM incidents WHERE id=?", (incident_id,), one=True)
    if not inc:
        raise WorkflowError("Incident not found", 404)
    if inc["status"] == "resolved":
        raise WorkflowError("This incident is resolved. Re-verify it first if the problem returned.")
    if query(f"SELECT 1 FROM actions WHERE incident_id=? AND status IN {OPEN_ACTION}", (incident_id,), one=True):
        raise WorkflowError("This incident already has an open plan. Approve or reject it first.")
    asset = asset_svc.incident_asset(incident_id)
    facts = {"category": inc["category"], "ward": inc["ward"], "report_count": inc["report_count"],
             "severity": inc["severity"], "priority_label": inc["priority_label"],
             "asset": f"{asset_svc.type_label(asset['asset_type'])} {asset['asset_code']}" if asset else None}
    plan = ai.recommend_action(facts)  # outside the lock
    with db(immediate=True) as conn:
        if conn.execute(f"SELECT 1 FROM actions WHERE incident_id=? AND status IN {OPEN_ACTION}",
                        (incident_id,)).fetchone():
            raise WorkflowError("Another plan was created meanwhile. Refresh.")
        cur = conn.execute(
            """INSERT INTO actions (incident_id, action_text, assigned_team, department, eta_hours,
                                    checklist, status, source, created_by, created_at)
               VALUES (?,?,?,?,?,?, 'pending', ?,?,?)""",
            (incident_id, plan["action_text"], plan.get("assigned_team"), plan.get("department"),
             plan.get("eta_hours", 24), json.dumps(plan.get("checklist", [])), plan.get("engine"),
             actor["id"], now()))
        _log(conn, actor, "plan_generated",
             f"New response plan drafted ({'AI-assisted' if plan.get('engine') == 'sarvam' else 'rule-based'})",
             incident_id)
    return query("SELECT * FROM actions WHERE id=?", (cur.lastrowid,), one=True)


def approve_action(action_id, actor, edits=None):
    fields = validate_edits(edits)
    with db(immediate=True) as conn:
        action, inc = _load_action(conn, action_id)
        _require(action, "pending")
        if inc["status"] == "resolved":
            raise WorkflowError("The incident is already resolved.")
        fields.update({"status": "approved", "approved_by": actor["id"], "approved_at": now()})
        _transition(conn, "actions", action_id, "pending", fields)
        conn.execute("UPDATE incidents SET status='in_progress', updated_at=? WHERE id=?", (now(), inc["id"]))
        _log(conn, actor, "action_approved",
             f"Plan #{action_id} approved{' with edits' if len(fields) > 3 else ''}", inc["id"])
        _notify_incident(conn, inc["id"], "Response approved",
                         "An officer approved a response plan for the issue you reported.", "approved")
    risk.safe_scan()
    return query("SELECT * FROM actions WHERE id=?", (action_id,), one=True)


def reject_action(action_id, actor, reason):
    reason = _clean_text(reason, "Rejection reason", 3, 300)
    with db(immediate=True) as conn:
        action, inc = _load_action(conn, action_id)
        _require(action, "pending")
        _transition(conn, "actions", action_id, "pending",
                    {"status": "rejected", "rejected_by": actor["id"], "rejected_at": now(),
                     "rejection_reason": reason})
        _log(conn, actor, "action_rejected", f"Plan #{action_id} rejected: {reason}", inc["id"])
    risk.safe_scan()
    return query("SELECT * FROM actions WHERE id=?", (action_id,), one=True)


def start_action(action_id, actor):
    with db(immediate=True) as conn:
        action, inc = _load_action(conn, action_id)
        _require(action, "approved")
        _transition(conn, "actions", action_id, "approved",
                    {"status": "in_progress", "started_at": now(), "started_by": actor["id"]})
        conn.execute("""UPDATE complaints SET status='in_progress', updated_at=?
                        WHERE incident_id=? AND status IN ('submitted','clustered')""", (now(), inc["id"]))
        _log(conn, actor, "action_started", f"Work started on plan #{action_id}", inc["id"])
        _notify_incident(conn, inc["id"], "Work in progress",
                         "The assigned team has started work on the issue you reported.", "in_progress")
    return query("SELECT * FROM actions WHERE id=?", (action_id,), one=True)


def complete_action(action_id, actor, notes, proof_image, accepted_proof=None, on_complete=None):
    """Complete a plan with a proof photo.

    `accepted_proof` is a photo already validated and stored as work-order
    evidence ({stored_name, image_hash, check}); it is used when an officer
    accepts a work order. `on_complete(conn)` runs inside the same transaction."""
    notes = (notes or "").strip()[:500]
    pre = query("SELECT a.status, a.incident_id FROM actions a WHERE a.id=?", (action_id,), one=True)
    if not pre:
        raise WorkflowError("Action not found", 404)
    _require(pre, "in_progress")
    if accepted_proof:
        proof = {"stored_name": accepted_proof["stored_name"], "image_hash": accepted_proof["image_hash"]}
        check = accepted_proof["check"]
        if not evidence.resolve_path(proof["stored_name"]):
            raise WorkflowError("The accepted completion photo is no longer on file.", 409)
    else:
        if not proof_image:
            raise WorkflowError("A proof-of-work photo is required to complete this plan.", 422)
        issue_hashes = [r["image_hash"] for r in query(
            "SELECT image_hash FROM complaints WHERE incident_id=? AND image_hash IS NOT NULL", (pre["incident_id"],))]
        inc_cat = query("SELECT category FROM incidents WHERE id=?", (pre["incident_id"],), one=True)["category"]
        try:
            proof = evidence.process(proof_image, "resolution", inc_cat, issue_hashes=issue_hashes)
        except evidence.EvidenceError as e:
            raise WorkflowError(e.message, e.status)
        if proof["verdict"] == "rejected":
            raise WorkflowError("The proof photo did not pass the automated checks.", 422,
                                {"reasons": proof["reasons"]})
        check = json.dumps({k: v for k, v in proof.items() if k != "stored_name"})
    try:
        with db(immediate=True) as conn:
            action, inc = _load_action(conn, action_id)
            _require(action, "in_progress")
            _transition(conn, "actions", action_id, "in_progress", {
                "status": "completed", "completed_at": now(), "completed_by": actor["id"], "notes": notes,
                "proof_path": proof["stored_name"], "proof_check": check, "image_hash": proof["image_hash"]})
            _log(conn, actor, "action_completed", f"Work completed on plan #{action_id}; proof photo submitted", inc["id"])
            # Record the work on the linked asset so maintenance history stays real.
            asset = asset_svc.incident_asset(inc["id"], conn=conn)
            if asset:
                asset_svc.add_maintenance(conn, asset["id"], {
                    "event_type": "repair", "performed_at": now(), "outcome": "completed",
                    "performed_by": action["assigned_team"] or action["department"],
                    "notes": f"{inc['public_id']}: {action['action_text']}"[:500],
                    "data_source": "CivicMind workflow", "is_synthetic": 0,
                }, recorded_by=actor["id"], action_id=action_id)
            if on_complete:
                on_complete(conn)
            result = _verify(conn, inc["id"], actor, "workflow_completion")
    except Exception:
        if not accepted_proof:  # work-order evidence stays on file with its own record
            evidence.discard(proof["stored_name"])
        raise
    risk.safe_scan()
    return result


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def _recurrence(conn, inc, since):
    """New related reports near the incident filed after `since`, not part of it."""
    rows = conn.execute("""SELECT * FROM complaints WHERE created_at >= ? AND status != 'rejected'
                           AND (incident_id IS NULL OR incident_id != ?)""", (since, inc["id"])).fetchall()
    return [dict(r) for r in rows
            if ai.categories_related(r["category"], inc["category"])
            and geo.haversine_km(r["latitude"], r["longitude"], inc["centroid_lat"], inc["centroid_lng"]) <= CLUSTER_RADIUS_KM]


def _verify(conn, incident_id, actor, verification_type):
    inc = dict(conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())
    action = conn.execute("""SELECT * FROM actions WHERE incident_id=? AND status='completed'
                             ORDER BY completed_at DESC, id DESC LIMIT 1""", (incident_id,)).fetchone()
    if not action:
        raise WorkflowError("Verification needs a completed plan with an accepted proof photo.")
    action = dict(action)
    check = json.loads(action["proof_check"] or "{}")
    if not action["proof_path"] or check.get("verdict") in (None, "rejected", "flagged") \
            or not evidence.resolve_path(action["proof_path"]):
        raise WorkflowError("Verification needs an accepted proof photo on file for the completed plan.")

    recurring = [r for r in _recurrence(conn, inc, action["completed_at"]) if r["incident_id"] is None]
    status = "reopened" if recurring else "resolved"
    who = actor["name"]
    summary = (f"Plan #{action['id']} completed by {who}; proof photo {check.get('verdict')}. "
               f"Recurrence check: {len(recurring)} new related report(s) within {CLUSTER_RADIUS_KM:g} km "
               f"since completion. {VERIFY_NOTE}")
    conn.execute(
        """INSERT INTO resolutions (incident_id, action_id, before_metric, after_metric, reduction_pct,
               resolution_confidence, summary, proof_path, proof_check, image_hash, resolved_by, status,
               verification_type, verified_at)
           VALUES (?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?)""",
        (incident_id, action["id"], inc["report_count"], len(recurring), summary, action["proof_path"],
         action["proof_check"], action["image_hash"], actor["id"],
         "verified" if status == "resolved" else "reopened", verification_type, now()))
    if status == "resolved":
        conn.execute("UPDATE incidents SET status='resolved', resolved_at=?, updated_at=? WHERE id=?",
                     (now(), now(), incident_id))
        conn.execute("""UPDATE complaints SET status='resolved', resolved_at=?, resolved_by=?, updated_at=?
                        WHERE incident_id=? AND status != 'resolved'""", (now(), actor["id"], now(), incident_id))
        _log(conn, actor, "resolution_verified", f"{inc['public_id']} resolved — {VERIFY_NOTE}", incident_id)
        _notify_incident(conn, incident_id, "Issue resolved",
                         f"Officer {who} completed the work and submitted a proof photo. "
                         "If the problem returns, please report it again.", "resolved")
    else:
        conn.execute("UPDATE incidents SET status='reopened', resolved_at=NULL, updated_at=? WHERE id=?",
                     (now(), incident_id))
        conn.execute("""UPDATE complaints SET status='clustered', resolved_at=NULL, updated_at=?
                        WHERE incident_id=?""", (now(), incident_id))
        for r in recurring:
            conn.execute("UPDATE complaints SET incident_id=?, status='clustered', updated_at=? WHERE id=?",
                         (incident_id, now(), r["id"]))
            conn.execute("INSERT OR IGNORE INTO incident_complaints (incident_id, complaint_id) VALUES (?,?)",
                         (incident_id, r["id"]))
        clustering.refresh_incident(conn, incident_id)
        _log(conn, actor, "incident_reopened",
             f"{inc['public_id']} reopened — {len(recurring)} new related report(s) after the work", incident_id)
        _notify_incident(conn, incident_id, "Issue reopened",
                         "New reports suggest the problem has returned. The incident was reopened.", "incident")
    return dict(conn.execute("SELECT * FROM resolutions WHERE incident_id=? ORDER BY id DESC LIMIT 1",
                             (incident_id,)).fetchone())


def verify_incident(incident_id, actor):
    """Officer-triggered re-check (e.g. a few days after the work)."""
    with db(immediate=True) as conn:
        inc = conn.execute("SELECT status FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not inc:
            raise WorkflowError("Incident not found", 404)
        if inc["status"] not in ("resolved", "in_progress"):
            raise WorkflowError(f"Incident is '{inc['status']}'; only resolved or in-progress incidents "
                                "with a completed plan can be verified.")
        result = _verify(conn, incident_id, actor, "recurrence_recheck")
    risk.safe_scan()
    return result


# ---------------------------------------------------------------------------
# Direct resolution of a single complaint
# ---------------------------------------------------------------------------
def resolve_complaint(complaint_id, actor, notes="", proof_image=None, plan=None, eta_hours=None):
    c = query("SELECT * FROM complaints WHERE id=?", (complaint_id,), one=True)
    if not c:
        raise WorkflowError("Complaint not found", 404)
    if c["status"] in ("resolved", "rejected"):
        raise WorkflowError(f"This complaint is already {c['status']}.")
    if not proof_image:
        raise WorkflowError("A proof-of-work photo is required to resolve this complaint.", 422)
    try:
        proof = evidence.process(proof_image, "resolution", c["category"],
                                 issue_hashes=[c["image_hash"]] if c["image_hash"] else [])
    except evidence.EvidenceError as e:
        raise WorkflowError(e.message, e.status)
    if proof["verdict"] == "rejected":
        raise WorkflowError("The proof photo did not pass the automated checks.", 422, {"reasons": proof["reasons"]})
    notes = (notes or "").strip()[:500]
    safe_plan = None
    if isinstance(plan, dict):
        safe_plan = {k: plan.get(k) for k in ("action_text", "assigned_team", "department", "eta_hours",
                                              "checklist", "engine") if k in plan}
    eta = None
    if eta_hours is not None:
        try:
            eta = max(1.0, min(720.0, float(eta_hours)))
        except (TypeError, ValueError):
            eta = None
    incident_closed = False
    try:
        with db(immediate=True) as conn:
            cur = conn.execute(
                """UPDATE complaints SET status='resolved', resolution_proof_path=?, resolution_proof_check=?,
                       resolution_image_hash=?, resolved_by=?, resolved_at=?, resolution_notes=?,
                       resolution_plan=?, eta_hours=?, updated_at=?
                   WHERE id=? AND status NOT IN ('resolved','rejected')""",
                (proof["stored_name"], json.dumps({k: v for k, v in proof.items() if k != "stored_name"}),
                 proof["image_hash"], actor["id"], now(), notes, json.dumps(safe_plan) if safe_plan else None,
                 eta, now(), complaint_id))
            if cur.rowcount != 1:
                raise WorkflowError("The complaint changed while you were working. Refresh.")
            conn.execute("""INSERT INTO notifications (user_id, complaint_id, incident_id, title, message, type, created_at)
                            VALUES (?,?,?,?,?, 'resolved', ?)""",
                         (c["user_id"], complaint_id, c["incident_id"], "Issue resolved",
                          f"Your report {c['public_id']} was resolved by officer {actor['name']} with a proof photo.",
                          now()))
            _log(conn, actor, "complaint_resolved", f"{c['public_id']} resolved directly with a proof photo",
                 c["incident_id"], complaint_id)
            if c["incident_id"]:
                remaining = conn.execute("SELECT COUNT(*) n FROM complaints WHERE incident_id=? AND status!='resolved'",
                                         (c["incident_id"],)).fetchone()["n"]
                if remaining == 0:
                    inc = conn.execute("SELECT * FROM incidents WHERE id=?", (c["incident_id"],)).fetchone()
                    conn.execute("UPDATE incidents SET status='resolved', resolved_at=?, updated_at=? WHERE id=?",
                                 (now(), now(), c["incident_id"]))
                    conn.execute(f"""UPDATE actions SET status='cancelled', rejection_reason=?
                                     WHERE incident_id=? AND status IN {OPEN_ACTION}""",
                                 ("Closed: every linked report was resolved directly", c["incident_id"]))
                    conn.execute(
                        """INSERT INTO resolutions (incident_id, complaint_id, before_metric, after_metric,
                               summary, proof_path, proof_check, image_hash, resolved_by, status,
                               verification_type, verified_at)
                           VALUES (?,?,?,0,?,?,?,?,?, 'verified', 'all_reports_resolved', ?)""",
                        (c["incident_id"], complaint_id, inc["report_count"],
                         f"All {inc['report_count']} linked reports were resolved individually with proof photos. "
                         + VERIFY_NOTE, proof["stored_name"], json.dumps({"verdict": proof["verdict"],
                                                                          "reasons": proof["reasons"],
                                                                          "engine": proof["engine"]}),
                         proof["image_hash"], actor["id"], now()))
                    _log(conn, actor, "incident_closed", f"{inc['public_id']} closed — all linked reports resolved",
                         c["incident_id"])
                    incident_closed = True
    except Exception:
        evidence.discard(proof["stored_name"])
        raise
    risk.safe_scan()
    return {"complaint_id": complaint_id, "public_id": c["public_id"], "status": "resolved",
            "officer_name": actor["name"], "photo": evidence.public_check(proof),
            "incident_closed": incident_closed}
