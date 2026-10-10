"""
CivicMind — infrastructure risk & conflict detection (rule engine).

Every finding is derived from rows in the database at scan time — nothing is
hard-coded. Each rule documents its trigger condition, and each finding
stores the record ids that triggered it plus a plain-language explanation.

Rules (thresholds in data/civicmind_config.json -> risk_rules):
  A  repeated_asset_complaints  >= N open complaints matched to one asset within W days
  B  repeat_after_maintenance   a complaint matched to an asset filed within L days AFTER a
                                recorded repair/replacement/cleaning (possible recurrence —
                                NOT proof that the repair failed)
  C  critical_asset_incident    an open incident with severity >= S linked to a 'critical' asset
  D  geographic_concentration   >= K open incidents of related categories within R km
                                (flagged for review; incidents are NOT merged automatically)
  E  overdue_inspection         last inspection older than the asset's inspection interval
                                (only for assets that have both values)
  F  conflicting_open_actions   open action plans on DIFFERENT incidents target the same asset

Findings are upserted by a deterministic key, so re-scanning is idempotent.
A finding whose condition no longer holds is marked 'cleared'. Human review
states ('acknowledged', 'dismissed') are preserved while the condition holds.
"""
import json
from datetime import timedelta

from config import RISK_RULES
from database import db, now, parse_ts, utcnow
from services import ai, assets as asset_svc, geo

OPEN_COMPLAINT = "c.status NOT IN ('resolved','rejected')"
OPEN_INCIDENT = "status NOT IN ('resolved')"
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
RULE_TITLES = {
    "A": "Repeated unresolved asset complaints",
    "B": "Repeat complaint after maintenance",
    "C": "Critical infrastructure incident",
    "D": "Geographic complaint concentration",
    "E": "Overdue inspection",
    "F": "Conflicting open actions",
}


def _rule(name, default=None):
    cfg = RISK_RULES.get(name, {})
    if default is not None:
        return cfg if cfg else default
    return cfg


def _enabled(name):
    return bool(RISK_RULES.get(name, {}).get("enabled", True))


def _bump(sev, asset):
    """Raise severity one step for assets marked critical."""
    if asset and asset.get("criticality") == "critical":
        order = ["low", "medium", "high", "critical"]
        return order[min(3, order.index(sev) + 1)]
    return sev


def _label(a):
    return f"{asset_svc.type_label(a['asset_type'])} {a['asset_code']}"


# ---------------------------------------------------------------------------
# Rules — each returns a list of finding dicts
# ---------------------------------------------------------------------------
def rule_a(conn, ts):
    if not _enabled("repeated_asset_complaints"):
        return []
    cfg = _rule("repeated_asset_complaints")
    min_n = int(cfg.get("min_open_complaints", 3))
    window = int(cfg.get("window_days", 30))
    since = (ts - timedelta(days=window)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(f"""
        SELECT ca.asset_id, GROUP_CONCAT(c.id) AS ids, COUNT(*) AS n, MAX(c.is_synthetic) AS syn
        FROM complaint_assets ca JOIN complaints c ON c.id = ca.complaint_id
        WHERE ca.status != 'rejected' AND {OPEN_COMPLAINT} AND c.created_at >= ?
        GROUP BY ca.asset_id HAVING COUNT(*) >= ?""", (since, min_n)).fetchall()
    out = []
    for r in rows:
        a = dict(conn.execute("SELECT * FROM assets WHERE id=?", (r["asset_id"],)).fetchone())
        ids = sorted(int(x) for x in r["ids"].split(","))
        out.append({
            "finding_key": f"A:{a['id']}", "rule_id": "A", "finding_type": "repeated_asset_complaints",
            "severity": _bump("medium", a), "asset_id": a["id"], "incident_id": None,
            "supporting": {"complaint_ids": ids},
            "explanation": (f"Repeated unresolved reports: {r['n']} open complaints have been matched to "
                            f"{_label(a)} within the last {window} days (threshold: {min_n})."),
            "recommended_step": f"Inspect {a['asset_code']} as one job and check for an underlying fault "
                                "rather than closing the reports one by one.",
            "is_synthetic": int(bool(a["is_synthetic"] or r["syn"])),
        })
    return out


def rule_b(conn, ts):
    if not _enabled("repeat_after_maintenance"):
        return []
    cfg = _rule("repeat_after_maintenance")
    lookback = int(cfg.get("lookback_days", 90))
    types = cfg.get("event_types") or ["repair", "replacement", "cleaning"]
    rows = conn.execute(f"""
        SELECT m.id AS mid, m.asset_id, m.event_type, m.performed_at, m.is_synthetic AS msyn,
               c.id AS cid, c.created_at, c.is_synthetic AS csyn
        FROM asset_maintenance m
        JOIN complaint_assets ca ON ca.asset_id = m.asset_id AND ca.status != 'rejected'
        JOIN complaints c ON c.id = ca.complaint_id
        WHERE m.event_type IN ({','.join('?' * len(types))})
          AND c.created_at > m.performed_at
          AND julianday(c.created_at) - julianday(m.performed_at) <= ?
          AND c.status != 'rejected'""", (*types, lookback)).fetchall()
    # Attribute each complaint to the MOST RECENT qualifying maintenance event only.
    latest = {}
    for r in rows:
        cur = latest.get(r["cid"])
        if cur is None or r["performed_at"] > cur["performed_at"]:
            latest[r["cid"]] = r
    groups = {}
    for r in latest.values():
        groups.setdefault(r["mid"], []).append(r)
    out = []
    for mid, items in groups.items():
        first = items[0]
        a = dict(conn.execute("SELECT * FROM assets WHERE id=?", (first["asset_id"],)).fetchone())
        ids = sorted(i["cid"] for i in items)
        gap = min((parse_ts(i["created_at"]) - parse_ts(i["performed_at"])).days for i in items)
        when = first["performed_at"][:10]
        out.append({
            "finding_key": f"B:{a['id']}:{mid}", "rule_id": "B", "finding_type": "repeat_after_maintenance",
            "severity": _bump("medium", a), "asset_id": a["id"], "incident_id": None,
            "supporting": {"complaint_ids": ids, "maintenance_ids": [mid]},
            "explanation": (f"Possible recurrence: {len(ids)} report(s) matched to {_label(a)} were filed "
                            f"{gap} or more day(s) after a recorded {first['event_type']} on {when}. "
                            "This indicates a potential recurrence, not proof that the work failed."),
            "recommended_step": f"Review the {first['event_type']} record for {a['asset_code']} and check "
                                "whether the same fault has returned.",
            "is_synthetic": int(bool(a["is_synthetic"] or first["msyn"] or any(i["csyn"] for i in items))),
        })
    return out


def _open_incidents_with_assets(conn):
    incs = [dict(r) for r in conn.execute(f"SELECT * FROM incidents WHERE {OPEN_INCIDENT}").fetchall()]
    for i in incs:
        i["_asset"] = asset_svc.incident_asset(i["id"], conn=conn)
    return incs


def rule_c(conn, ts, incs):
    if not _enabled("critical_asset_incident"):
        return []
    min_sev = int(_rule("critical_asset_incident").get("min_severity", 3))
    out = []
    for i in incs:
        a = i["_asset"]
        if not a or a.get("criticality") != "critical" or i["severity"] < min_sev:
            continue
        reason = f" ({a['criticality_reason']})" if a.get("criticality_reason") else ""
        out.append({
            "finding_key": f"C:{i['id']}:{a['id']}", "rule_id": "C", "finding_type": "critical_asset_incident",
            "severity": "critical", "asset_id": a["id"], "incident_id": i["id"],
            "supporting": {"incident_ids": [i["id"]], "matched_reports": a["matched_reports"]},
            "explanation": (f"Open incident {i['public_id']} (severity {i['severity']}/5) is linked to "
                            f"{_label(a)}, which is marked critical{reason}."),
            "recommended_step": "Prioritise dispatch and consider an interim safety measure while the repair is planned.",
            "is_synthetic": int(bool(a["is_synthetic"])),
        })
    return out


def rule_d(conn, ts, incs):
    if not _enabled("geographic_concentration"):
        return []
    cfg = _rule("geographic_concentration")
    radius, min_k = float(cfg.get("radius_km", 2.0)), int(cfg.get("min_incidents", 3))
    n = len(incs)
    adj = {k: set() for k in range(n)}
    for x in range(n):
        for y in range(x + 1, n):
            a, b = incs[x], incs[y]
            if ai.categories_related(a["category"], b["category"]) and geo.haversine_km(
                    a["centroid_lat"], a["centroid_lng"], b["centroid_lat"], b["centroid_lng"]) <= radius:
                adj[x].add(y)
                adj[y].add(x)
    seen, out = set(), []
    for s in range(n):
        if s in seen:
            continue
        comp, stack = [], [s]
        seen.add(s)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in adj[cur]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        if len(comp) < min_k:
            continue
        members = sorted((incs[k] for k in comp), key=lambda i: -i["priority_score"])
        ids = sorted(i["id"] for i in members)
        cats = sorted({i["category"] for i in members})
        out.append({
            "finding_key": "D:" + ",".join(map(str, ids)), "rule_id": "D",
            "finding_type": "geographic_concentration", "severity": "medium",
            "asset_id": None, "incident_id": members[0]["id"],
            "supporting": {"incident_ids": ids},
            "explanation": (f"{len(ids)} open incidents of related categories ({', '.join(cats)}) are "
                            f"within {radius:g} km of each other. They were NOT merged automatically."),
            "recommended_step": "Review whether these incidents share a root cause and can be handled by one team.",
            "is_synthetic": 0,
        })
    return out


def rule_e(conn, ts):
    if not _enabled("overdue_inspection"):
        return []
    out = []
    rows = conn.execute("""SELECT * FROM assets WHERE inspection_interval_days IS NOT NULL
                           AND last_inspected_at IS NOT NULL
                           AND operational_status != 'decommissioned'""").fetchall()
    for r in rows:
        a = dict(r)
        last = parse_ts(a["last_inspected_at"])
        if not last:
            continue
        overdue = (ts - last).days - int(a["inspection_interval_days"])
        if overdue <= 0:
            continue
        sev = {"critical": "high", "high": "medium"}.get(a["criticality"], "low")
        out.append({
            "finding_key": f"E:{a['id']}", "rule_id": "E", "finding_type": "overdue_inspection",
            "severity": sev, "asset_id": a["id"], "incident_id": None,
            "supporting": {"last_inspected_at": a["last_inspected_at"],
                           "inspection_interval_days": a["inspection_interval_days"]},
            "explanation": (f"{_label(a)} was last inspected on {a['last_inspected_at'][:10]}; its "
                            f"inspection interval is {a['inspection_interval_days']} days, so it is "
                            f"{overdue} day(s) overdue."),
            "recommended_step": f"Schedule an inspection of {a['asset_code']} and record it in the maintenance log.",
            "is_synthetic": int(bool(a["is_synthetic"])),
        })
    return out


def rule_f(conn, ts, incs):
    if not _enabled("conflicting_open_actions"):
        return []
    by_asset = {}
    for i in incs:
        a = i["_asset"]
        if not a:
            continue
        acts = conn.execute("""SELECT id FROM actions WHERE incident_id=?
                               AND status IN ('pending','approved','in_progress')""", (i["id"],)).fetchall()
        for act in acts:
            by_asset.setdefault(a["id"], {"asset": a, "items": []})["items"].append((i, act["id"]))
    out = []
    for asset_id, g in by_asset.items():
        incident_ids = sorted({i["id"] for i, _ in g["items"]})
        if len(incident_ids) < 2:
            continue  # several plans on ONE incident are sequential revisions, not a conflict
        action_ids = sorted(aid for _, aid in g["items"])
        a = g["asset"]
        out.append({
            "finding_key": f"F:{asset_id}:" + ",".join(map(str, action_ids)), "rule_id": "F",
            "finding_type": "conflicting_open_actions", "severity": "medium",
            "asset_id": asset_id, "incident_id": incident_ids[0],
            "supporting": {"incident_ids": incident_ids, "action_ids": action_ids},
            "explanation": (f"{len(action_ids)} open action plans on {len(incident_ids)} different incidents "
                            f"target {_label(a)}. They may duplicate or conflict with each other."),
            "recommended_step": "Coordinate the plans: keep one work order for the asset and link the incidents to it.",
            "is_synthetic": int(bool(a["is_synthetic"])),
        })
    return out


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def scan():
    """Evaluate every rule against current data and persist the findings."""
    ts = utcnow()
    stamp = now()
    with db(immediate=True) as conn:
        incs = _open_incidents_with_assets(conn)
        found = (rule_a(conn, ts) + rule_b(conn, ts) + rule_c(conn, ts, incs)
                 + rule_d(conn, ts, incs) + rule_e(conn, ts) + rule_f(conn, ts, incs))
        keys = set()
        created = 0
        for f in found:
            keys.add(f["finding_key"])
            sup = json.dumps(f["supporting"], sort_keys=True)
            row = conn.execute("SELECT id, status FROM risk_findings WHERE finding_key=?",
                               (f["finding_key"],)).fetchone()
            if row:
                status = "open" if row["status"] == "cleared" else row["status"]
                conn.execute("""UPDATE risk_findings SET severity=?, supporting=?, explanation=?,
                                recommended_step=?, status=?, is_synthetic=?, incident_id=?, updated_at=?
                                WHERE id=?""",
                             (f["severity"], sup, f["explanation"], f["recommended_step"], status,
                              f["is_synthetic"], f["incident_id"], stamp, row["id"]))
            else:
                conn.execute("""INSERT INTO risk_findings (finding_key, rule_id, finding_type, severity,
                                asset_id, incident_id, supporting, explanation, recommended_step,
                                status, is_synthetic, detected_at, updated_at)
                                VALUES (?,?,?,?,?,?,?,?,?, 'open', ?,?,?)""",
                             (f["finding_key"], f["rule_id"], f["finding_type"], f["severity"],
                              f["asset_id"], f["incident_id"], sup, f["explanation"],
                              f["recommended_step"], f["is_synthetic"], stamp, stamp))
                created += 1
        cleared = 0
        for row in conn.execute("SELECT id, finding_key FROM risk_findings "
                                "WHERE status IN ('open','acknowledged')").fetchall():
            if row["finding_key"] not in keys:
                conn.execute("UPDATE risk_findings SET status='cleared', updated_at=? WHERE id=?",
                             (stamp, row["id"]))
                cleared += 1
    return {"evaluated_at": stamp, "active": len(found), "new": created, "cleared": cleared}


def safe_scan():
    try:
        return scan()
    except Exception as exc:  # noqa: BLE001 — never break the calling workflow
        print(f"[CivicMind] risk scan skipped: {type(exc).__name__}")
        return None


def finding_out(r):
    r = dict(r)
    try:
        r["supporting"] = json.loads(r.get("supporting") or "{}")
    except ValueError:
        r["supporting"] = {}
    r.pop("finding_key", None)
    r["rule_title"] = RULE_TITLES.get(r["rule_id"], r["rule_id"])
    return r
