"""CivicMind — dashboard metrics and chart data.

Every metric is computed from rows at request time and has a stated
definition (returned in `definitions` so the UI can show it). Percentages
with a zero denominator are returned as null, not 0.
"""
import time
from datetime import timedelta

from fastapi import APIRouter, Depends

from auth import require_officer, require_citizen
from database import query, parse_ts, utcnow
from services import clustering, workorders as wo_svc
from routes.common import safe_json

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

OPEN_C = "status NOT IN ('resolved','rejected')"
OPEN_I = "status != 'resolved'"
DEFINITIONS = {
    "open_complaints": "Complaints not yet resolved or rejected.",
    "open_incidents": "Incidents (groups of related reports) not yet resolved.",
    "high_priority_incidents": "Open incidents labelled High or Critical by the priority policy.",
    "actions_awaiting_approval": "Response plans in 'pending' state.",
    "actions_in_progress": "Plans approved or under way, not yet completed.",
    "open_risk_findings": "Infrastructure risk findings that are open or acknowledged.",
    "resolution_rate": "Resolved complaints ÷ all non-rejected complaints. Null when there are none.",
    "median_resolution_hours": "Median hours from submission to resolution, resolved complaints only.",
    "reporting_citizens": "Distinct citizens with at least one open complaint.",
    "overdue_work_orders": "Assigned or in-progress work orders past their estimated completion.",
    "awaiting_verification": "Work orders reported complete and waiting for an officer's review.",
    "open_work_orders": "Work orders assigned or in progress.",
}
_last_refresh = [0.0]


def _c(sql, params=()):
    return query(sql, params, one=True)["c"]


@router.get("")
def officer_dashboard(user: dict = Depends(require_officer)):
    # Age-dependent priority factors drift over time; refresh at most every 5 minutes.
    if time.time() - _last_refresh[0] > 300:
        _last_refresh[0] = time.time()
        try:
            clustering.refresh_open_priorities()
        except Exception:  # noqa: BLE001
            pass
    total = _c("SELECT COUNT(*) c FROM complaints WHERE status != 'rejected'")
    resolved = _c("SELECT COUNT(*) c FROM complaints WHERE status='resolved'")
    hours = []
    for r in query("SELECT created_at, resolved_at FROM complaints WHERE status='resolved' AND resolved_at IS NOT NULL"):
        a, b = parse_ts(r["created_at"]), parse_ts(r["resolved_at"])
        if a and b and b >= a:
            hours.append((b - a).total_seconds() / 3600)
    hours.sort()
    median = None
    if hours:
        mid = len(hours) // 2
        median = round(hours[mid] if len(hours) % 2 else (hours[mid - 1] + hours[mid]) / 2, 1)

    queue = query(f"""SELECT id, public_id, title, category, priority_score, priority_label, priority_factors,
                      ward, status, report_count, created_at, asset_id FROM incidents WHERE {OPEN_I}
                      ORDER BY priority_score DESC LIMIT 15""")
    for q in queue:
        q["reasons"] = (safe_json(q.pop("priority_factors"), {}) or {}).get("reasons", [])
        q["asset_code"] = None
        if q["asset_id"]:
            a = query("SELECT asset_code FROM assets WHERE id=?", (q["asset_id"],), one=True)
            q["asset_code"] = a["asset_code"] if a else None

    findings = query("""SELECT f.id, f.rule_id, f.severity, f.explanation, f.incident_id, f.status, f.is_synthetic,
                               a.asset_code FROM risk_findings f LEFT JOIN assets a ON a.id=f.asset_id
                        WHERE f.status IN ('open','acknowledged')
                        ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                 WHEN 'medium' THEN 2 ELSE 3 END, f.detected_at DESC LIMIT 8""")
    return {
        "metrics": {
            "total_complaints": total,
            "open_complaints": _c(f"SELECT COUNT(*) c FROM complaints WHERE {OPEN_C}"),
            "resolved_complaints": resolved,
            "open_incidents": _c(f"SELECT COUNT(*) c FROM incidents WHERE {OPEN_I}"),
            "high_priority_incidents": _c(f"SELECT COUNT(*) c FROM incidents WHERE {OPEN_I} "
                                          "AND priority_label IN ('Critical','High')"),
            "resolved_incidents": _c("SELECT COUNT(*) c FROM incidents WHERE status='resolved'"),
            "actions_awaiting_approval": _c("SELECT COUNT(*) c FROM actions WHERE status='pending'"),
            "actions_in_progress": _c("SELECT COUNT(*) c FROM actions WHERE status IN ('approved','in_progress')"),
            "open_risk_findings": _c("SELECT COUNT(*) c FROM risk_findings WHERE status IN ('open','acknowledged')"),
            "resolution_rate": round(resolved / total * 100, 1) if total else None,
            "median_resolution_hours": median,
            "reporting_citizens": _c(f"SELECT COUNT(DISTINCT user_id) c FROM complaints WHERE {OPEN_C}"),
            "synthetic_complaints": _c("SELECT COUNT(*) c FROM complaints WHERE is_synthetic=1"),
            "overdue_work_orders": wo_svc.overdue_count(),
            "awaiting_verification": _c("SELECT COUNT(*) c FROM work_orders WHERE status='awaiting_verification'"),
            "open_work_orders": _c("SELECT COUNT(*) c FROM work_orders WHERE status IN ('assigned','in_progress')"),
        },
        "definitions": DEFINITIONS,
        "recent_reports": _recent_reports(),
        "priority_queue": queue,
        "findings": findings,
        "recent_activity": query("""SELECT actor, action, detail, incident_id, created_at FROM activity
                                    ORDER BY id DESC LIMIT 15"""),
    }


@router.get("/charts")
def charts(user: dict = Depends(require_officer)):
    trend = []
    today = utcnow().date()
    for i in range(6, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        trend.append({"day": day,
                      "created": _c("SELECT COUNT(*) c FROM complaints WHERE date(created_at)=?", (day,)),
                      "resolved": _c("SELECT COUNT(*) c FROM complaints WHERE date(resolved_at)=?", (day,))})
    return {
        "categories": query("SELECT category, COUNT(*) c FROM complaints GROUP BY category ORDER BY c DESC"),
        "priorities": query(f"SELECT priority_label label, COUNT(*) c FROM incidents WHERE {OPEN_I} GROUP BY priority_label"),
        "by_ward": query("SELECT COALESCE(ward,'Unmapped') ward, COUNT(*) c FROM complaints GROUP BY ward ORDER BY c DESC LIMIT 10"),
        "trend": trend,
    }


@router.get("/citizen")
def citizen_dashboard(user: dict = Depends(require_citizen)):
    uid = (user["id"],)
    return {
        "my_complaints": _c("SELECT COUNT(*) c FROM complaints WHERE user_id=?", uid),
        "resolved": _c("SELECT COUNT(*) c FROM complaints WHERE user_id=? AND status='resolved'", uid),
        "in_progress": _c("SELECT COUNT(*) c FROM complaints WHERE user_id=? AND status IN ('clustered','in_progress')", uid),
        "unread_notifications": _c("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0", uid),
        "incidents_joined": _c("SELECT COUNT(DISTINCT incident_id) c FROM complaints WHERE user_id=? AND incident_id IS NOT NULL", uid),
    }


def _recent_reports():
    return query("""
        SELECT c.id AS complaint_id, c.public_id, c.category, c.ward, c.status, c.severity, c.created_at,
               c.incident_id, c.proof_verified, c.image_path IS NOT NULL AS has_photo, c.resolved_at,
               c.is_synthetic, u.name AS citizen_name, o.name AS officer_name
        FROM complaints c LEFT JOIN users u ON u.id = c.user_id LEFT JOIN users o ON o.id = c.resolved_by
        ORDER BY c.created_at DESC, c.id DESC LIMIT 25""")
