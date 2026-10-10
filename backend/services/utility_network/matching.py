"""
CivicMind — candidate utility assets for an incident.

A candidate is a COMPATIBLE asset (by complaint category -> utility type)
whose recorded geometry lies within MATCH_RADIUS_M of a report. Every
candidate carries its distance and the reason; none implies causation.
Officers confirm or reject candidates; rejected ones stay rejected.
"""
from database import query, db, now
from services.utility_network import geometry, model, registry


def _report_points(incident_id):
    pts = [(r["latitude"], r["longitude"]) for r in
           query("SELECT latitude, longitude FROM complaints WHERE incident_id=?", (incident_id,))]
    inc = query("SELECT centroid_lat, centroid_lng FROM incidents WHERE id=?", (incident_id,), one=True)
    if inc:
        pts.append((inc["centroid_lat"], inc["centroid_lng"]))
    return pts


def compute(incident, radius_m=model.MATCH_RADIUS_M, limit=8):
    utilities = model.CATEGORY_UTILITIES.get(incident["category"], [])
    if not utilities:
        return [], utilities
    pts = _report_points(incident["id"])
    if not pts:
        return [], utilities
    lat0 = sum(p[0] for p in pts) / len(pts)
    dlat, dlng = geometry.degree_margin(radius_m, lat0)
    min_lat, max_lat = min(p[0] for p in pts) - dlat, max(p[0] for p in pts) + dlat
    min_lng, max_lng = min(p[1] for p in pts) - dlng, max(p[1] for p in pts) + dlng
    rows = query(f"""SELECT {registry.ASSET_COLS} FROM utility_assets a
                     WHERE a.utility_type IN ({','.join('?' * len(utilities))})
                     AND a.max_lat >= ? AND a.min_lat <= ? AND a.max_lng >= ? AND a.min_lng <= ?""",
                 (*utilities, min_lat, max_lat, min_lng, max_lng))
    out = []
    for a in rows:
        d = min(geometry.distance_m(lat, lng, a["geometry"]) for lat, lng in pts)
        if d > radius_m:
            continue
        label = model.UTILITY_TYPES[a["utility_type"]]["label"].lower()
        out.append({"asset": a, "distance_m": round(d, 1),
                    "reason": (f"{incident['category']} is compatible with {label} assets; recorded "
                               f"{a['asset_type'].replace('_', ' ')} geometry lies {round(d)} m from a report "
                               f"(search radius {radius_m} m). Proximity only — not evidence of cause.")})
    # Lines before points of the same distance: a pipe is a likelier subject than a valve on it.
    out.sort(key=lambda c: (c["distance_m"], c["asset"]["geometry_type"] != "LineString"))
    return out[:limit], utilities


def refresh(incident):
    """Store newly computed candidates (never overwriting an officer's confirm/reject)."""
    found, utilities = compute(incident)
    with db() as conn:
        for c in found:
            conn.execute("""INSERT OR IGNORE INTO incident_utility_assets
                            (incident_id, asset_id, status, match_method, distance_m, reason, created_at)
                            VALUES (?,?, 'candidate', 'compatible_type_within_radius', ?,?,?)""",
                         (incident["id"], c["asset"]["id"], c["distance_m"], c["reason"], now()))
    return found, utilities


def context(incident):
    """Utility context for the incident workspace (contract: GET /api/incidents/{id}/utility-context)."""
    found, utilities = refresh(incident)
    stored = {r["asset_id"]: r for r in query(
        """SELECT iua.*, u.name AS reviewer FROM incident_utility_assets iua
           LEFT JOIN users u ON u.id=iua.reviewed_by WHERE iua.incident_id=?""", (incident["id"],))}
    items = []
    seen = set()
    for c in found:
        s = stored.get(c["asset"]["id"], {})
        seen.add(c["asset"]["id"])
        items.append(registry.summary(c["asset"]) | {
            "distance_m": c["distance_m"], "reason": c["reason"], "link_status": s.get("status", "candidate"),
            "reviewed_by": s.get("reviewer"), "reviewed_at": s.get("reviewed_at")})
    # Officer-confirmed assets stay listed even if outside the current radius.
    for pk, s in stored.items():
        if pk not in seen and s["status"] == "confirmed":
            a = registry.get_row_by_pk(pk)
            items.append(registry.summary(a) | {"distance_m": s["distance_m"], "reason": s["reason"],
                                                "link_status": "confirmed", "reviewed_by": s.get("reviewer"),
                                                "reviewed_at": s.get("reviewed_at")})
    gaps = []
    if not utilities:
        gaps.append(f"No utility network type is associated with the category '{incident['category']}'.")
    elif not items:
        gaps.append(f"No recorded {', '.join(utilities)} asset lies within {model.MATCH_RADIUS_M} m of the reports.")
    return {"incident_id": incident["id"], "public_id": incident["public_id"], "category": incident["category"],
            "utility_types": utilities, "radius_m": model.MATCH_RADIUS_M,
            "candidates": [i for i in items if i["link_status"] != "rejected"],
            "rejected": [i for i in items if i["link_status"] == "rejected"],
            "data_gaps": gaps,
            "note": "Candidates are nearby compatible assets. Proximity does not establish cause; confirm on site."}


def review(incident_id, asset_code, status, user):
    a = registry.get_row(asset_code)
    if not a:
        return None
    with db(immediate=True) as conn:
        cur = conn.execute("""UPDATE incident_utility_assets SET status=?, reviewed_by=?, reviewed_at=?
                              WHERE incident_id=? AND asset_id=?""", (status, user["id"], now(), incident_id, a["id"]))
        if cur.rowcount == 0:
            conn.execute("""INSERT INTO incident_utility_assets (incident_id, asset_id, status, match_method, reason,
                            reviewed_by, reviewed_at, created_at) VALUES (?,?,?, 'officer_selected', ?,?,?,?)""",
                         (incident_id, a["id"], status, "Selected by an officer", user["id"], now(), now()))
        conn.execute("""INSERT INTO activity (actor, actor_id, action, detail, incident_id, created_at)
                        VALUES (?,?,?,?,?,?)""",
                     (user["name"], user["id"], "utility_asset_" + status,
                      f"Utility asset {a['asset_id']} marked {status} for this incident", incident_id, now()))
    return True
