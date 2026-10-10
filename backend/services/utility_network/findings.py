"""
CivicMind — rule-based utility findings. Each finding names its rule, the
records that triggered it and a plain explanation. Re-scanning is idempotent:
findings are upserted by key and cleared when their trigger disappears.

  U1 repeated_reports        >= 3 open reports in incidents linked to one asset (30 days)
  U2 recurrence_after_work   open incident on an asset within 90 days after a repair/replacement
  U3 critical_asset_incident high/critical asset linked to an open incident of severity >= 4
  U4 unverified_trace        isolation candidates for a linked asset rely on unverified records
  U5 missing_source_link     upstream trace of a linked asset reaches no recorded source
  U6 conflicting_actions     two open incidents with open action plans linked to the same asset
"""
import json
from datetime import timedelta

from database import query, db, now, parse_ts, utcnow
from services.utility_network import graph, matching, model, registry

RULES = {
    "U1": "Repeated reports on one utility asset",
    "U2": "Problem reported soon after recorded work",
    "U3": "Critical utility asset involved in a severe incident",
    "U4": "Isolation candidates rely on unverified records",
    "U5": "No recorded source upstream (network gap)",
    "U6": "Conflicting open action plans on one asset",
}
OPEN_INC = "status != 'resolved'"


def _linked():
    """(asset row, incident row) pairs, one focus per open incident: the officer-confirmed assets, or —
    if none is confirmed yet — only the single nearest candidate (lines before points). Rules never fire
    for every asset that merely happens to be nearby."""
    pairs = []
    for i in query(f"SELECT * FROM incidents WHERE {OPEN_INC}"):
        matching.refresh(i)
        links = query("""SELECT iua.asset_id AS pk, iua.status, iua.distance_m, a.geometry_type
                         FROM incident_utility_assets iua JOIN utility_assets a ON a.id=iua.asset_id
                         WHERE iua.incident_id=? AND iua.status != 'rejected'""", (i["id"],))
        confirmed = [x for x in links if x["status"] == "confirmed"]
        focus = confirmed or sorted(links, key=lambda x: (x["distance_m"] if x["distance_m"] is not None else 1e9,
                                                          x["geometry_type"] != "LineString"))[:1]
        for x in focus:
            a = registry.get_row_by_pk(x["pk"])
            if a:
                pairs.append((a, dict(i, link_status=x["status"])))
    return pairs


def scan():
    pairs = _linked()
    by_asset = {}
    for a, inc in pairs:
        by_asset.setdefault(a["asset_id"], (a, []))[1].append(inc)
    found = {}

    def add(key, rule, severity, assets, incidents, supporting, explanation, synthetic):
        found[key] = {"rule_id": rule, "severity": severity, "asset_ids": assets, "incident_ids": incidents,
                      "supporting": supporting, "explanation": explanation, "is_synthetic": int(bool(synthetic))}

    since = (utcnow() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    for code, (a, incs) in by_asset.items():
        inc_ids = sorted({i["id"] for i in incs})
        comps = query(f"""SELECT id, public_id FROM complaints WHERE incident_id IN ({','.join('?' * len(inc_ids))})
                          AND status NOT IN ('resolved','rejected') AND created_at >= ?""", (*inc_ids, since))
        if len(comps) >= 3:
            add(f"U1:{code}", "U1", "critical" if a["criticality"] == "critical" else "high", [code], inc_ids,
                {"complaint_ids": [c["public_id"] for c in comps]},
                f"{len(comps)} open reports in the last 30 days are in incidents linked to {code} ({a['name']}).",
                a["is_synthetic"])
        for i in incs:
            first = parse_ts(i["created_at"])
            work = query("""SELECT record_id, work_type, performed_on FROM utility_maintenance WHERE asset_id=?
                            AND work_type IN ('repair','replacement') ORDER BY performed_on DESC""", (a["id"],))
            for w in work:
                done = parse_ts(w["performed_on"])
                if first and done and timedelta(0) <= first - done <= timedelta(days=90):
                    add(f"U2:{code}:{i['id']}", "U2", "high", [code], [i["id"]], {"maintenance_ids": [w["record_id"]]},
                        f"{i['public_id']} was reported {(first - done).days} days after recorded "
                        f"{w['work_type']} {w['record_id']} on {code}. The earlier work may not have held.",
                        a["is_synthetic"])
                    break
            if a["criticality"] in ("high", "critical") and (i["severity"] or 0) >= 4:
                add(f"U3:{code}:{i['id']}", "U3", "critical" if a["criticality"] == "critical" else "high", [code],
                    [i["id"]], {}, f"{code} is recorded as {a['criticality']} criticality and is linked to "
                    f"{i['public_id']} (severity {i['severity']}/5).", a["is_synthetic"])
        if model.has_isolation(a["utility_type"]):
            iso = graph.isolation(code)
            weak = sorted({r for c in iso["upstream_candidates"] + iso["downstream_candidates"]
                           for r in c["unverified_records"]})
            if weak:
                add(f"U4:{code}", "U4", "medium", [code], inc_ids, {"record_ids": weak},
                    f"Candidate isolation devices for {code} depend on unverified records ({', '.join(weak[:6])}). "
                    "Confirm with the operator before relying on the trace.", a["is_synthetic"])
        if model.UTILITY_TYPES.get(a["utility_type"], {}).get("source_types"):
            up = graph.trace(code, "upstream")
            if not up["possible_sources"] and not up["trace_limits"]["hop_limit_reached"]:
                add(f"U5:{code}", "U5", "medium", [code], inc_ids, {"dead_ends": up["dead_ends"]},
                    f"No recorded source is reachable upstream of {code}. A connection is missing from the "
                    "records, so the supply path cannot be established from data.", a["is_synthetic"])
        with_actions = [i["id"] for i in incs if query(
            "SELECT 1 FROM actions WHERE incident_id=? AND status IN ('pending','approved','in_progress')",
            (i["id"],), one=True)]
        if len(with_actions) >= 2:
            add(f"U6:{code}", "U6", "high", [code], sorted(with_actions), {},
                f"{len(with_actions)} open incidents have open action plans on {code}; coordinate them.",
                a["is_synthetic"])

    ts, created, cleared = now(), 0, 0
    with db(immediate=True) as conn:
        existing = {r["finding_key"]: r for r in conn.execute("SELECT finding_key, status FROM utility_findings")}
        for key, f in found.items():
            vals = (f["rule_id"], f["severity"], json.dumps(f["asset_ids"]), json.dumps(f["incident_ids"]),
                    json.dumps(f["supporting"]), f["explanation"], f["is_synthetic"], ts)
            if key in existing:
                conn.execute("""UPDATE utility_findings SET rule_id=?, severity=?, asset_ids=?, incident_ids=?,
                                supporting=?, explanation=?, is_synthetic=?, updated_at=?,
                                status=CASE WHEN status='cleared' THEN 'open' ELSE status END
                                WHERE finding_key=?""", (*vals, key))
            else:
                created += 1
                conn.execute("""INSERT INTO utility_findings (rule_id, severity, asset_ids, incident_ids, supporting,
                                explanation, is_synthetic, updated_at, finding_key, created_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?)""", (*vals, key, ts))
        for key, r in existing.items():
            if key not in found and r["status"] in ("open", "acknowledged"):
                cleared += 1
                conn.execute("UPDATE utility_findings SET status='cleared', updated_at=? WHERE finding_key=?", (ts, key))
    return {"evaluated_at": ts, "active": len(found), "new": created, "cleared": cleared}


def safe_scan():
    try:
        return scan()
    except Exception as exc:  # noqa: BLE001 — findings must never break the request that triggered them
        print(f"[CivicMind] utility findings scan skipped: {type(exc).__name__}")
        return None


def finding_out(r):
    return {"id": r["id"], "rule_id": r["rule_id"], "rule_title": RULES.get(r["rule_id"], r["rule_id"]),
            "severity": r["severity"], "asset_ids": json.loads(r["asset_ids"] or "[]"),
            "incident_ids": json.loads(r["incident_ids"] or "[]"), "supporting": json.loads(r["supporting"] or "{}"),
            "explanation": r["explanation"], "status": r["status"], "is_synthetic": bool(r["is_synthetic"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"], "review_note": r["review_note"]}


def list_findings(status="active", asset_id=None, incident_id=None):
    sql, params = "SELECT * FROM utility_findings WHERE 1=1", []
    if status == "active":
        sql += " AND status IN ('open','acknowledged')"
    elif status != "all":
        sql += " AND status=?"
        params.append(status)
    rows = [finding_out(r) for r in query(sql + """ ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                                   WHEN 'medium' THEN 2 ELSE 3 END, updated_at DESC LIMIT 300""",
                                          tuple(params))]
    if asset_id:
        rows = [r for r in rows if asset_id in r["asset_ids"]]
    if incident_id:
        rows = [r for r in rows if incident_id in r["incident_ids"]]
    return rows
