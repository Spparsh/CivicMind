"""
CivicMind — deterministic SYNTHETIC utility network for demonstrations.

Everything here is fictional: provenance `synthetic_demo`, is_synthetic=1,
"(fictional)" in names, and contacts that cannot be dialled. It is written
only by the explicit demo seed command (seed_demo.py) into the isolated demo
database, never on application start-up. Re-running it adds nothing new; it
only moves existing synthetic records onto the current street layout.

The networks follow REAL street centre-lines of Kothrud, Pune (OpenStreetMap
extract, ODbL — see street_network.py); the utility records themselves are
invented. Water / sewer / storm / electricity / gas / telecom each sit at their
own position across the road.

Core water scenario (fixed IDs, used by the leak walkthrough and tests):
  SRC-DEMO-01 reservoir → WM-DEMO-01 trunk → J-DEMO-01 → V-DEMO-01 → WM-DEMO-02 → J-DEMO-02
     J-DEMO-02 → V-DEMO-02 → WM-DEMO-03 (leak street, "Lane 7") → J-DEMO-03 → V-DEMO-04 → WM-DEMO-07 → END-DEMO-01
     J-DEMO-02 → WM-DEMO-04 → V-DEMO-03 → WM-DEMO-05 → J-DEMO-04
     J-DEMO-03 – WM-DEMO-06 – J-DEMO-04   (loop; flow direction unknown)
  Old main WM-DEMO-101 → WM-DEMO-102 has NO recorded upstream link, although
  WM-DEMO-101 starts ~10 m from J-DEMO-03 on the map.
The rest of the area is served by separate fictional supply zones (ESR-D2…D5).
"""
import json
from datetime import datetime, timedelta, timezone

from services.utility_network import geometry, model, street_network

SOURCE_CODE = "SRC-SYNTHETIC-DEMO"
ORG = {"water": "Fictional Water Utility (demo)", "electric": "Fictional Power Utility (demo)",
       "sewer": "Fictional Sewerage Board (demo)", "storm": "Fictional Roads & Drainage Dept (demo)",
       "gas": "Fictional City Gas Distributor (demo)", "telecom": "Fictional Fibre Operator (demo)"}

CONTACTS = [
    ("CT-DEMO-01", "Water Supply Control Room — FICTIONAL", "24×7 control room (demo)",
     "DEMO-ONLY · not a real number", "water", ORG["water"], True, "verified"),
    ("CT-DEMO-02", "Ward water engineer — FICTIONAL", "Section engineer (demo)",
     "DEMO-ONLY · not a real number", "water", ORG["water"], False, "unverified"),
    ("CT-DEMO-03", "Power distribution desk — FICTIONAL", "Outage desk (demo)",
     "DEMO-ONLY · not a real number", "electric", ORG["electric"], True, "verified"),
]

MAINTENANCE = [
    ("MR-DEMO-01", "WM-DEMO-03", "repair", 25, "Leaking joint repaired near Lane 7 (fictional)", "WO-DEMO-118"),
    ("MR-DEMO-02", "V-DEMO-02", "inspection", 200, "Valve exercised, operable (fictional)", "WO-DEMO-064"),
    ("MR-DEMO-03", "SW-DEMO-01", "inspection", 120, "RMU inspected (fictional)", "WO-DEMO-090"),
]

_LEAK_TEXTS = [
    (0, "Water is gushing out of the road on Lane 7 near the tea stall. The road is flooding.", 6),
    (1, "Big water leak from under the road in Lane 7, water flowing towards the main road.", 4),
    (2, "लेन 7 मध्ये रस्त्याखालून पाणी वाहत आहे, पाईप फुटली आहे असे वाटते.", 2),
]

_built = None


def _build():
    global _built
    if _built is None:
        assets, conns, info = street_network.build()
        reports = [(who, text, lat, lng, hours)
                   for (who, text, hours), (lat, lng) in zip(_LEAK_TEXTS, info["leak_reports"])]
        _built = {"ASSETS": assets, "CONNECTIONS": conns, "LEAK_POINT": info["leak_point"],
                  "LEAK_REPORTS": reports, "STREETS_ATTRIBUTION": info["attribution"]}
    return _built


def __getattr__(name):
    """ASSETS, CONNECTIONS, LEAK_POINT, LEAK_REPORTS, STREETS_ATTRIBUTION — built on first use."""
    if name in ("ASSETS", "CONNECTIONS", "LEAK_POINT", "LEAK_REPORTS", "STREETS_ATTRIBUTION"):
        return _build()[name]
    raise AttributeError(name)


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def seed(conn):
    """Idempotent: inserts rows whose IDs are missing and moves existing synthetic
    records onto the current street layout. Returns counts of new rows."""
    data = _build()
    ts = _ago(0)
    row = conn.execute("SELECT id FROM utility_data_sources WHERE source_code=?", (SOURCE_CODE,)).fetchone()
    if row:
        source_id = row["id"]
    else:
        source_id = conn.execute(
            """INSERT INTO utility_data_sources (source_code, name, source_type, authority, license_notes, crs,
               provenance_class, is_synthetic, created_at) VALUES (?,?, 'synthetic', ?,?, 'EPSG:4326',
               'synthetic_demo', 1, ?)""",
            (SOURCE_CODE, "CivicMind synthetic demo network (fictional)", "None — fictional dataset",
             "Generated for demonstrations. Not real infrastructure. Laid along street centre-lines from "
             + data["STREETS_ATTRIBUTION"], ts)).lastrowid
    new = {"assets": 0, "connections": 0, "contacts": 0, "maintenance": 0}
    existing = {r["asset_id"]: r for r in conn.execute(
        "SELECT id, asset_id, geometry, name, is_synthetic, source_id FROM utility_assets")}
    inserts, moves = [], []
    for aid, ut, at, name, geom, extra in data["ASSETS"]:
        gtype, gnorm, (min_lat, min_lng, max_lat, max_lng) = geometry.validate(geom)
        gjson = json.dumps(gnorm)
        old = existing.get(aid)
        if old:
            if old["is_synthetic"] and old["source_id"] == source_id and (old["geometry"] != gjson or old["name"] != name):
                moves.append((gtype, gjson, min_lat, min_lng, max_lat, max_lng, name, ts, old["id"]))
            continue
        verified = extra.get("verification_status", "verified") == "verified"
        inserts.append((
            aid, ut, at, name, gtype, gjson, min_lat, min_lng, max_lat, max_lng,
            ORG.get(ut, f"Fictional {ut} operator (demo)"), extra.get("criticality"),
            json.dumps(extra.get("attributes", {})), int(model.is_control(ut, at)), int(model.is_source(ut, at)),
            "restricted" if ut in ("gas", "electric") else "internal", source_id,
            extra.get("provenance", "synthetic_demo"), extra.get("verification_status", "verified"),
            _ago(60) if verified else None, ts, ts))
    conn.executemany(
        """INSERT INTO utility_assets (asset_id, utility_type, asset_type, name, geometry_type, geometry, min_lat,
           min_lng, max_lat, max_lng, operational_status, operator_org, criticality, attributes, is_control_device,
           is_source, sensitivity, source_id, provenance_class, verification_status, is_synthetic,
           last_verified_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'in_service', ?,?,?,?,?,?,?,?,?, 1, ?,?,?)""", inserts)
    conn.executemany("""UPDATE utility_assets SET geometry_type=?, geometry=?, min_lat=?, min_lng=?, max_lat=?,
                        max_lng=?, name=?, updated_at=? WHERE id=?""", moves)
    new["assets"] = len(inserts)
    if moves:
        new["moved_onto_streets"] = len(moves)

    ids = {r["asset_id"]: r["id"] for r in conn.execute("SELECT id, asset_id FROM utility_assets")}
    have = {r["connection_id"] for r in conn.execute("SELECT connection_id FROM utility_connections")}
    rows = [(cid, ids[f], ids[t], ctype, direction, source_id, extra.get("verification_status", "verified"),
             extra.get("notes"), ts)
            for cid, f, t, ctype, direction, extra in data["CONNECTIONS"] if cid not in have]
    conn.executemany(
        """INSERT INTO utility_connections (connection_id, from_asset, to_asset, connection_type, direction,
           source_id, provenance_class, verification_status, is_synthetic, notes, created_at)
           VALUES (?,?,?,?,?,?, 'synthetic_demo', ?, 1, ?, ?)""", rows)
    new["connections"] = len(rows)

    for cid, org, role, phone, ut, operator, emergency, status in CONTACTS:
        if conn.execute("SELECT 1 FROM utility_contacts WHERE contact_id=?", (cid,)).fetchone():
            continue
        conn.execute(
            """INSERT INTO utility_contacts (contact_id, organization, role, phone, is_emergency, hours, utility_type,
               operator_org, source_id, verification_status, last_verified_at, visibility_level, is_synthetic, created_at)
               VALUES (?,?,?,?,?, '24×7 (demo)', ?,?,?,?,?, 'officer', 1, ?)""",
            (cid, org, role, phone, int(emergency), ut, operator, source_id, status,
             _ago(30) if status == "verified" else None, ts))
        new["contacts"] += 1
    for rid, aid, work, days, outcome, ref in MAINTENANCE:
        if conn.execute("SELECT 1 FROM utility_maintenance WHERE record_id=?", (rid,)).fetchone():
            continue
        conn.execute(
            """INSERT INTO utility_maintenance (record_id, asset_id, work_type, performed_on, outcome, reference,
               department, source_id, provenance_class, is_synthetic, created_at)
               VALUES (?,?,?,?,?,?, 'Fictional crew (demo)', ?, 'synthetic_demo', 1, ?)""",
            (rid, ids[aid], work, _ago(days), outcome, ref, source_id, ts))
        new["maintenance"] += 1
    return new


def relocate_leak_reports(conn):
    """Move the fictional leak reports of an older demo database onto the leak street.
    Returns the ids of incidents whose reports moved (their utility candidates need a refresh)."""
    moved_incidents = set()
    for _who, text, lat, lng, _hours in _build()["LEAK_REPORTS"]:
        for r in conn.execute("""SELECT id, incident_id FROM complaints WHERE is_synthetic=1 AND description=?
                                 AND (abs(latitude-?) > 1e-7 OR abs(longitude-?) > 1e-7)""",
                              (text, lat, lng)).fetchall():
            conn.execute("UPDATE complaints SET latitude=?, longitude=? WHERE id=?", (lat, lng, r["id"]))
            if r["incident_id"]:
                moved_incidents.add(r["incident_id"])
    for iid in moved_incidents:
        conn.execute("""UPDATE incidents SET
                        centroid_lat=(SELECT AVG(latitude) FROM complaints WHERE incident_id=:i),
                        centroid_lng=(SELECT AVG(longitude) FROM complaints WHERE incident_id=:i) WHERE id=:i""",
                     {"i": iid})
    return sorted(moved_incidents)
