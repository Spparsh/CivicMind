"""
CivicMind — incident grouping (complaint clustering).

Deterministic and explainable. Two complaints are linked when they are
  (a) within CLUSTER_RADIUS_KM of each other,
  (b) filed within CLUSTER_TIME_WINDOW_HOURS of each other, and
  (c) of the same category or categories listed together in config
      `category_groups` (deliberately narrow — a pothole is never merged
      with a streetlight report just because it is nearby).
Connected components of at least MIN_CLUSTER_SIZE complaints become an incident.

A new complaint that matches an EXISTING open incident (same rules, measured
against the incident centre and its latest report) joins that incident
instead of creating a duplicate.
"""
import json

from config import CLUSTER_RADIUS_KM, CLUSTER_TIME_WINDOW_HOURS, MIN_CLUSTER_SIZE
from database import query, db, now, parse_ts, insert_with_public_id, utcnow
from services import ai, geo, priority, assets as asset_svc

OPEN_INCIDENT_STATUSES = ("active", "in_progress", "reopened")


def grouping_basis(categories):
    return {
        "method": "connected components of pairwise links",
        "radius_km": CLUSTER_RADIUS_KM, "window_hours": CLUSTER_TIME_WINDOW_HOURS,
        "min_reports": MIN_CLUSTER_SIZE, "categories": sorted(categories),
    }


def _hours_between(a, b):
    ta, tb = parse_ts(a), parse_ts(b)
    if not ta or not tb:
        return float("inf")
    return abs((ta - tb).total_seconds()) / 3600.0


def _linked(a, b):
    return (ai.categories_related(a["category"], b["category"])
            and geo.haversine_km(a["latitude"], a["longitude"], b["latitude"], b["longitude"]) <= CLUSTER_RADIUS_KM
            and _hours_between(a["created_at"], b["created_at"]) <= CLUSTER_TIME_WINDOW_HOURS)


def _components(items):
    n = len(items)
    adj = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            if _linked(items[i], items[j]):
                adj[i].add(j)
                adj[j].add(i)
    seen, comps = set(), []
    for start in range(n):
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in adj[cur]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        comps.append(comp)
    return comps


def _dominant_category(members):
    counts = {}
    for m in members:
        counts[m["category"]] = counts.get(m["category"], 0) + 1
    return max(sorted(counts), key=counts.get)


# ---------------------------------------------------------------------------
# Entry point after a complaint is filed
# ---------------------------------------------------------------------------
def place_complaint(complaint_id):
    """Attach the complaint to a matching open incident, or try to form a new
    incident from unlinked complaints. Returns {"incident_id", "created"}."""
    c = query("SELECT * FROM complaints WHERE id=?", (complaint_id,), one=True)
    if not c or c["incident_id"] or c["status"] in ("resolved", "rejected"):
        return {"incident_id": c["incident_id"] if c else None, "created": False}
    target = _matching_open_incident(c)
    if target:
        attach(target["id"], c["id"])
        return {"incident_id": target["id"], "created": False}
    created = detect_incidents()
    row = query("SELECT incident_id FROM complaints WHERE id=?", (complaint_id,), one=True)
    return {"incident_id": row["incident_id"] if row else None, "created": bool(created)}


def _matching_open_incident(c):
    best = None
    for inc in query(f"SELECT * FROM incidents WHERE status IN {OPEN_INCIDENT_STATUSES}"):
        if not ai.categories_related(c["category"], inc["category"]):
            continue
        d = geo.haversine_km(c["latitude"], c["longitude"], inc["centroid_lat"], inc["centroid_lng"])
        if d > CLUSTER_RADIUS_KM:
            continue
        if _hours_between(c["created_at"], inc["last_report_at"] or inc["created_at"]) > CLUSTER_TIME_WINDOW_HOURS:
            continue
        if best is None or d < best[1]:
            best = (inc, d)
    return best[0] if best else None


def attach(incident_id, complaint_id):
    with db(immediate=True) as conn:
        inc = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        c = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        if not inc or not c or c["incident_id"]:
            return False
        has_work = conn.execute("""SELECT 1 FROM actions WHERE incident_id=? AND status='in_progress'""",
                                (incident_id,)).fetchone()
        conn.execute("UPDATE complaints SET incident_id=?, status=?, updated_at=? WHERE id=?",
                     (incident_id, "in_progress" if has_work else "clustered", now(), complaint_id))
        conn.execute("INSERT OR IGNORE INTO incident_complaints (incident_id, complaint_id) VALUES (?,?)",
                     (incident_id, complaint_id))
        refresh_incident(conn, incident_id)
        conn.execute("""INSERT INTO activity (actor, action, detail, incident_id, complaint_id)
                        VALUES ('CivicMind', 'report_linked', ?, ?, ?)""",
                     (f"Report {c['public_id']} joined {inc['public_id']}", incident_id, complaint_id))
        conn.execute("""INSERT INTO notifications (user_id, complaint_id, incident_id, title, message, type)
                        VALUES (?,?,?,?,?, 'incident')""",
                     (c["user_id"], complaint_id, incident_id, "Linked to an existing incident",
                      f"Your report {c['public_id']} matches incident {inc['public_id']} "
                      f"({inc['title']}), which is already being handled."))
    return True


# ---------------------------------------------------------------------------
# Batch detection
# ---------------------------------------------------------------------------
def detect_incidents():
    """Group unlinked open complaints into new incidents. Returns created incidents."""
    complaints = query("""SELECT * FROM complaints WHERE incident_id IS NULL
                          AND status NOT IN ('resolved','rejected') ORDER BY created_at""")
    if len(complaints) < MIN_CLUSTER_SIZE:
        return []
    comps = [c for c in _components(complaints) if len(c) >= MIN_CLUSTER_SIZE]
    comps.sort(key=len, reverse=True)
    created = []
    for comp in comps:
        inc = _create_incident([complaints[i] for i in comp])
        if inc:
            created.append(inc)
    return created


def _members_asset(member_ids):
    if not member_ids:
        return None
    return query(f"""SELECT a.*, COUNT(*) AS matched_reports FROM complaint_assets ca
                     JOIN assets a ON a.id=ca.asset_id
                     WHERE ca.complaint_id IN ({','.join('?' * len(member_ids))}) AND ca.status != 'rejected'
                     GROUP BY a.id ORDER BY matched_reports DESC LIMIT 1""", tuple(member_ids), one=True)


def _create_incident(members):
    ids = [m["id"] for m in members]
    category = _dominant_category(members)
    points = [(m["latitude"], m["longitude"]) for m in members]
    clat, clng = geo.centroid(points)
    area = geo.map_area(clat, clng)
    cats = sorted({m["category"] for m in members})
    asset = _members_asset(ids)
    facts = {
        "category": category, "categories": cats, "report_count": len(members),
        "spread_km": geo.spread_km(points),
        "duration_hours": round(_hours_between(min(m["created_at"] for m in members),
                                               max(m["created_at"] for m in members)), 1),
        "ward": area["ward"], "zone": area["zone"],
        "severity": round(sum(m["severity"] for m in members) / len(members)),
        "asset": f"{asset_svc.type_label(asset['asset_type'])} {asset['asset_code']}" if asset else None,
    }
    # Model calls happen OUTSIDE the write lock.
    investigation = ai.investigate_incident(facts)
    plan = ai.recommend_action(facts)
    title = f"Probable {category} — {area['ward'] or 'unmapped area'}"

    with db(immediate=True) as conn:
        # Re-check membership inside the lock: another request may have
        # grouped some of these complaints in the meantime.
        free = [r["id"] for r in conn.execute(
            f"SELECT id FROM complaints WHERE id IN ({','.join('?' * len(ids))}) AND incident_id IS NULL",
            ids).fetchall()]
        if len(free) < MIN_CLUSTER_SIZE:
            return None
        incident_id, public_id = insert_with_public_id(
            conn, "incidents", "INC",
            ["title", "type", "category", "severity", "status", "ward", "zone", "centroid_lat",
             "centroid_lng", "report_count", "summary", "probable_cause", "investigation", "grouping_basis"],
            [title, category, category, facts["severity"], "active", area["ward"], area["zone"],
             clat, clng, len(free), investigation.get("summary"), investigation.get("probable_cause"),
             json.dumps(investigation), json.dumps(grouping_basis(cats))])
        for cid in free:
            conn.execute("UPDATE complaints SET incident_id=?, status='clustered', updated_at=? WHERE id=?",
                         (incident_id, now(), cid))
            conn.execute("INSERT OR IGNORE INTO incident_complaints (incident_id, complaint_id) VALUES (?,?)",
                         (incident_id, cid))
        refresh_incident(conn, incident_id)
        for label, val in [
            ("report_count", f"{len(free)} linked reports"),
            ("geographic_spread", f"{facts['spread_km']} km from centre"),
            ("time_window", f"{facts['duration_hours']} hours between first and last report"),
            ("categories", ", ".join(cats)),
        ] + ([("infrastructure_asset", facts["asset"])] if asset else []):
            conn.execute("INSERT INTO evidence (incident_id, evidence_type, value, source) VALUES (?,?,?,?)",
                         (incident_id, label, val, "incident-grouping"))
        conn.execute(
            """INSERT INTO actions (incident_id, action_text, assigned_team, department, eta_hours,
                                    checklist, status, source) VALUES (?,?,?,?,?,?, 'pending', ?)""",
            (incident_id, plan["action_text"], plan.get("assigned_team"), plan.get("department"),
             plan.get("eta_hours", 24), json.dumps(plan.get("checklist", [])), plan.get("engine", "rules")))
        conn.execute("""INSERT INTO activity (actor, action, detail, incident_id)
                        VALUES ('CivicMind', 'incident_detected', ?, ?)""",
                     (f"{public_id}: {title} ({len(free)} reports grouped)", incident_id))
        for m in members:
            if m["id"] in free:
                conn.execute(
                    """INSERT INTO notifications (user_id, complaint_id, incident_id, title, message, type)
                       VALUES (?,?,?,?,?, 'incident')""",
                    (m["user_id"], m["id"], incident_id, "Grouped into an incident",
                     f"Your report {m['public_id']} was grouped with similar nearby reports into "
                     f"incident {public_id}. An officer will review it."))
    return query("SELECT * FROM incidents WHERE id=?", (incident_id,), one=True)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def refresh_incident(conn, incident_id):
    """Recompute an incident's metrics and priority from its CURRENT members."""
    members = [dict(r) for r in conn.execute(
        """SELECT c.* FROM complaints c JOIN incident_complaints ic ON ic.complaint_id=c.id
           WHERE ic.incident_id=?""", (incident_id,)).fetchall()]
    if not members:
        return
    inc = dict(conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())
    points = [(m["latitude"], m["longitude"]) for m in members]
    clat, clng = geo.centroid(points)
    first = min(m["created_at"] for m in members)
    last = max(m["created_at"] for m in members)
    severity = round(sum(m["severity"] for m in members) / len(members))
    asset = asset_svc.incident_asset(incident_id, conn=conn)
    open_or_unresolved_since = first
    p = priority.compute_priority(
        severity, len(members), open_or_unresolved_since, inc["category"],
        asset_code=asset["asset_code"] if asset else None,
        asset_criticality=asset["criticality"] if asset else None,
        now=parse_ts(inc["resolved_at"]) if inc.get("resolved_at") else utcnow())
    conn.execute(
        """UPDATE incidents SET report_count=?, centroid_lat=?, centroid_lng=?, spread_km=?,
               duration_hours=?, severity=?, affected_people=?, last_report_at=?, asset_id=?,
               priority_score=?, priority_label=?, priority_reason=?, priority_factors=?, updated_at=?
           WHERE id=?""",
        (len(members), clat, clng, geo.spread_km(points), round(_hours_between(first, last), 1),
         severity, len({m["user_id"] for m in members}), last, asset["id"] if asset else None,
         p["priority_score"], p["priority_label"], p["reason_text"], json.dumps(p), now(), incident_id))


def refresh_open_priorities():
    """Age-based factors grow over time; recompute open incidents on demand."""
    ids = [r["id"] for r in query(f"SELECT id FROM incidents WHERE status IN {OPEN_INCIDENT_STATUSES}")]
    if not ids:
        return 0
    with db(immediate=True) as conn:
        for i in ids:
            refresh_incident(conn, i)
    return len(ids)
