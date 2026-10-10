"""
CivicMind — infrastructure asset registry and complaint-to-asset matching.

Matching rule (deterministic, explainable):
  1. Only asset types compatible with the complaint category are considered
     (config `category_asset_types`; e.g. Streetlight Failure -> streetlight).
     A nearby asset of an unrelated type is NEVER matched, however close.
  2. Decommissioned assets are ignored.
  3. Among compatible assets within `asset_match.radius_m` of the complaint,
     the nearest one is chosen. If none is within the radius: no match.

Asset locations are points. Linear assets (road segments) are represented by
a reference point, which is a simplification and is labelled as such.
"""
import json
import re

from config import (ASSET_TYPES, CATEGORY_ASSET_TYPES, ASSET_MATCH_RADIUS_M)
from database import query, db, now, parse_ts, utcnow
from services import geo

CRITICALITIES = ("low", "medium", "high", "critical")
OPERATIONAL_STATUSES = ("operational", "faulty", "under_maintenance", "decommissioned")
EVENT_TYPES = ("inspection", "repair", "replacement", "cleaning")
MATCH_METHOD = "nearest_compatible_type_within_radius"
_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{1,31}$")


class AssetError(ValueError):
    pass


def compatible_types(category):
    return list(CATEGORY_ASSET_TYPES.get(category, []))


def type_label(asset_type):
    return (ASSET_TYPES.get(asset_type) or {}).get("label", asset_type)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_asset(data: dict) -> dict:
    if not isinstance(data, dict):
        raise AssetError("Asset must be an object")
    code = str(data.get("asset_code") or "").strip().upper()
    if not _CODE_RE.match(code):
        raise AssetError("asset_code must be 2-32 characters: A-Z, 0-9 or '-'")
    atype = str(data.get("asset_type") or "").strip()
    if atype not in ASSET_TYPES:
        raise AssetError(f"Unsupported asset_type '{atype[:30]}'")
    name = str(data.get("name") or "").strip()[:120] or f"{type_label(atype)} {code}"
    try:
        lat, lng = float(data.get("latitude")), float(data.get("longitude"))
    except (TypeError, ValueError):
        raise AssetError("latitude/longitude must be numbers")
    if not geo.valid_coordinates(lat, lng):
        raise AssetError("Invalid coordinates")
    if not geo.in_service_area(lat, lng):
        raise AssetError("Coordinates are outside the configured service area")
    crit = str(data.get("criticality") or "medium").lower()
    if crit not in CRITICALITIES:
        raise AssetError(f"criticality must be one of {', '.join(CRITICALITIES)}")
    status = str(data.get("operational_status") or "operational").lower()
    if status not in OPERATIONAL_STATUSES:
        raise AssetError(f"operational_status must be one of {', '.join(OPERATIONAL_STATUSES)}")
    interval = data.get("inspection_interval_days")
    if interval not in (None, ""):
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            raise AssetError("inspection_interval_days must be an integer")
        if not 1 <= interval <= 3650:
            raise AssetError("inspection_interval_days must be between 1 and 3650")
    else:
        interval = None
    last = data.get("last_inspected_at")
    if last not in (None, ""):
        parsed = parse_ts(last)
        if not parsed or parsed > utcnow():
            raise AssetError("last_inspected_at must be a past date (YYYY-MM-DD)")
        last = parsed.strftime("%Y-%m-%d %H:%M:%S")
    else:
        last = None
    source = str(data.get("data_source") or "").strip()[:120]
    if not source:
        raise AssetError("data_source is required (where does this record come from?)")
    return {
        "asset_code": code, "asset_type": atype, "name": name,
        "latitude": lat, "longitude": lng,
        "ward": geo.map_area(lat, lng)["ward"],
        "department": (ASSET_TYPES.get(atype) or {}).get("department"),
        "operational_status": status, "criticality": crit,
        "criticality_reason": str(data.get("criticality_reason") or "").strip()[:200] or None,
        "inspection_interval_days": interval, "last_inspected_at": last,
        "data_source": source, "is_synthetic": 1 if data.get("is_synthetic") else 0,
    }


def insert_asset(conn, a: dict):
    cols = list(a.keys())
    cur = conn.execute(
        f"INSERT INTO assets ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(a[c] for c in cols))
    return cur.lastrowid


def create_asset(data: dict):
    a = validate_asset(data)
    with db(immediate=True) as conn:
        if conn.execute("SELECT 1 FROM assets WHERE asset_code=?", (a["asset_code"],)).fetchone():
            raise AssetError(f"Asset code {a['asset_code']} already exists")
        return insert_asset(conn, a)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def find_match(category, lat, lng, conn=None):
    """Return the best compatible asset within radius, or None (pure function)."""
    types = compatible_types(category)
    if not types:
        return None
    sql = (f"SELECT * FROM assets WHERE asset_type IN ({','.join('?' * len(types))}) "
           "AND operational_status != 'decommissioned'")
    rows = [dict(r) for r in conn.execute(sql, types).fetchall()] if conn else query(sql, tuple(types))
    best = None
    for a in rows:
        d_m = geo.haversine_km(lat, lng, a["latitude"], a["longitude"]) * 1000
        if d_m <= ASSET_MATCH_RADIUS_M and (best is None or d_m < best[1]):
            best = (a, d_m)
    if not best:
        return None
    a, d_m = best
    return {
        "asset": a, "distance_m": round(d_m, 1), "method": MATCH_METHOD,
        "reason": (f"Nearest {type_label(a['asset_type']).lower()} compatible with "
                   f"'{category}', {d_m:.0f} m away (match radius {ASSET_MATCH_RADIUS_M:.0f} m)"),
    }


def match_complaint(conn, complaint_id, category, lat, lng):
    """Store (or clear) the asset match for a complaint inside a transaction."""
    existing = conn.execute("SELECT status FROM complaint_assets WHERE complaint_id=?",
                            (complaint_id,)).fetchone()
    if existing and existing["status"] in ("confirmed", "rejected"):
        return None  # a human decision is never overwritten automatically
    m = find_match(category, lat, lng, conn=conn)
    if not m:
        conn.execute("DELETE FROM complaint_assets WHERE complaint_id=?", (complaint_id,))
        return None
    conn.execute(
        """INSERT INTO complaint_assets (complaint_id, asset_id, distance_m, method, reason, status,
                                         matched_at, updated_at)
           VALUES (?,?,?,?,?, 'auto', ?, ?)
           ON CONFLICT(complaint_id) DO UPDATE SET asset_id=excluded.asset_id,
               distance_m=excluded.distance_m, method=excluded.method,
               reason=excluded.reason, updated_at=excluded.updated_at""",
        (complaint_id, m["asset"]["id"], m["distance_m"], m["method"], m["reason"], now(), now()))
    return m


def incident_asset(incident_id, conn=None):
    """Most frequently matched (non-rejected) asset among an incident's reports."""
    sql = """SELECT a.*, COUNT(*) AS matched_reports, MIN(ca.distance_m) AS min_distance_m
             FROM complaint_assets ca
             JOIN assets a ON a.id = ca.asset_id
             JOIN incident_complaints ic ON ic.complaint_id = ca.complaint_id
             WHERE ic.incident_id = ? AND ca.status != 'rejected'
             GROUP BY a.id ORDER BY matched_reports DESC, min_distance_m ASC LIMIT 1"""
    if conn:
        row = conn.execute(sql, (incident_id,)).fetchone()
        return dict(row) if row else None
    return query(sql, (incident_id,), one=True)


# ---------------------------------------------------------------------------
# Maintenance history
# ---------------------------------------------------------------------------
def validate_maintenance(data: dict) -> dict:
    etype = str(data.get("event_type") or "").lower()
    if etype not in EVENT_TYPES:
        raise AssetError(f"event_type must be one of {', '.join(EVENT_TYPES)}")
    performed = parse_ts(data.get("performed_at"))
    if not performed or performed > utcnow():
        raise AssetError("performed_at must be a past date (YYYY-MM-DD)")
    source = str(data.get("data_source") or "").strip()[:120]
    if not source:
        raise AssetError("data_source is required")
    return {
        "event_type": etype,
        "performed_at": performed.strftime("%Y-%m-%d %H:%M:%S"),
        "outcome": str(data.get("outcome") or "").strip()[:200] or None,
        "performed_by": str(data.get("performed_by") or "").strip()[:120] or None,
        "notes": str(data.get("notes") or "").strip()[:500] or None,
        "data_source": source,
        "is_synthetic": 1 if data.get("is_synthetic") else 0,
    }


def add_maintenance(conn, asset_id, m: dict, recorded_by=None, action_id=None):
    cur = conn.execute(
        """INSERT INTO asset_maintenance (asset_id, event_type, performed_at, outcome, performed_by,
                                          notes, data_source, is_synthetic, recorded_by, action_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (asset_id, m["event_type"], m["performed_at"], m["outcome"], m["performed_by"],
         m["notes"], m["data_source"], m["is_synthetic"], recorded_by, action_id))
    # Any field visit counts as an inspection for the overdue-inspection rule.
    conn.execute("""UPDATE assets SET last_inspected_at=?, updated_at=?
                    WHERE id=? AND (last_inspected_at IS NULL OR last_inspected_at < ?)""",
                 (m["performed_at"], now(), asset_id, m["performed_at"]))
    return cur.lastrowid


# ---------------------------------------------------------------------------
# GeoJSON import
# ---------------------------------------------------------------------------
MAX_IMPORT_FEATURES = 2000


def import_geojson(doc, data_source, is_synthetic=False):
    """Import Point features as assets. Existing asset codes are NEVER
    overwritten — they are reported as duplicates. Returns a summary."""
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection" \
            or not isinstance(doc.get("features"), list):
        raise AssetError("Expected a GeoJSON FeatureCollection")
    feats = doc["features"]
    if len(feats) > MAX_IMPORT_FEATURES:
        raise AssetError(f"Too many features (max {MAX_IMPORT_FEATURES})")
    summary = {"imported": 0, "duplicates": [], "rejected": [], "total": len(feats)}
    seen = set()
    with db(immediate=True) as conn:
        for i, f in enumerate(feats):
            try:
                if not isinstance(f, dict) or f.get("type") != "Feature":
                    raise AssetError("not a Feature")
                g = f.get("geometry") or {}
                if g.get("type") != "Point":
                    raise AssetError(f"unsupported geometry '{str(g.get('type'))[:20]}' (Point only)")
                c = g.get("coordinates")
                if not isinstance(c, list) or len(c) < 2:
                    raise AssetError("invalid coordinates")
                props = f.get("properties") or {}
                if not isinstance(props, dict):
                    raise AssetError("properties must be an object")
                a = validate_asset({**props, "longitude": c[0], "latitude": c[1],
                                    "data_source": props.get("data_source") or data_source,
                                    "is_synthetic": props.get("is_synthetic", is_synthetic)})
                if a["asset_code"] in seen or conn.execute(
                        "SELECT 1 FROM assets WHERE asset_code=?", (a["asset_code"],)).fetchone():
                    summary["duplicates"].append(a["asset_code"])
                    continue
                insert_asset(conn, a)
                seen.add(a["asset_code"])
                summary["imported"] += 1
            except AssetError as e:
                summary["rejected"].append({"index": i, "reason": str(e)[:160]})
    return summary


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------
def asset_public(a: dict) -> dict:
    if not a:
        return None
    out = {k: a.get(k) for k in (
        "id", "asset_code", "asset_type", "name", "latitude", "longitude", "ward", "department",
        "operational_status", "criticality", "criticality_reason", "inspection_interval_days",
        "last_inspected_at", "data_source", "is_synthetic")}
    out["type_label"] = type_label(a.get("asset_type"))
    out["inspection_overdue"] = inspection_overdue(a)
    return out


def inspection_overdue(a):
    if not a.get("inspection_interval_days") or not a.get("last_inspected_at"):
        return None  # unknown — no data, no claim
    last = parse_ts(a["last_inspected_at"])
    if not last:
        return None
    return (utcnow() - last).days > int(a["inspection_interval_days"])


def list_assets(asset_type=None):
    sql, params = "SELECT * FROM assets", []
    if asset_type:
        sql += " WHERE asset_type=?"
        params.append(asset_type)
    rows = query(sql + " ORDER BY asset_code", tuple(params))
    counts = {r["asset_id"]: r for r in query(
        """SELECT ca.asset_id, COUNT(*) AS total,
                  SUM(CASE WHEN c.status NOT IN ('resolved','rejected') THEN 1 ELSE 0 END) AS open
           FROM complaint_assets ca JOIN complaints c ON c.id=ca.complaint_id
           WHERE ca.status != 'rejected' GROUP BY ca.asset_id""")}
    findings = {r["asset_id"]: r["n"] for r in query(
        "SELECT asset_id, COUNT(*) n FROM risk_findings WHERE status='open' AND asset_id IS NOT NULL GROUP BY asset_id")}
    out = []
    for r in rows:
        a = asset_public(r)
        a["linked_complaints"] = (counts.get(r["id"]) or {}).get("total", 0) or 0
        a["open_complaints"] = (counts.get(r["id"]) or {}).get("open", 0) or 0
        a["open_findings"] = findings.get(r["id"], 0)
        out.append(a)
    return out


def asset_detail(asset_id):
    a = query("SELECT * FROM assets WHERE id=?", (asset_id,), one=True)
    if not a:
        return None
    out = asset_public(a)
    out["maintenance"] = query(
        """SELECT m.id, m.event_type, m.performed_at, m.outcome, m.performed_by, m.notes,
                  m.data_source, m.is_synthetic, m.action_id, u.name AS recorded_by_name
           FROM asset_maintenance m LEFT JOIN users u ON u.id=m.recorded_by
           WHERE m.asset_id=? ORDER BY m.performed_at DESC""", (asset_id,))
    out["complaints"] = query(
        """SELECT c.id, c.public_id, c.category, c.status, c.created_at, c.incident_id,
                  ca.distance_m, ca.reason, ca.status AS match_status
           FROM complaint_assets ca JOIN complaints c ON c.id=ca.complaint_id
           WHERE ca.asset_id=? ORDER BY c.created_at DESC LIMIT 100""", (asset_id,))
    out["findings"] = query(
        """SELECT id, rule_id, finding_type, severity, explanation, recommended_step, status,
                  detected_at, is_synthetic FROM risk_findings
           WHERE asset_id=? ORDER BY status='open' DESC, detected_at DESC""", (asset_id,))
    return out


def supporting_json(obj):
    return json.dumps(obj, sort_keys=True)
