"""
CivicMind — utility data import: PREVIEW (validate only) -> CONFIRM (admin, transactional).

Formats
  * GeoJSON FeatureCollection of Point / LineString features (EPSG:4326,
    [longitude, latitude]). Optional top-level "connections" list:
    {"from_asset_id", "to_asset_id", "connection_type", "direction"}.
  * CSV of point assets (asset_id, utility_type, asset_type, name, lat, lng, ...)
    or of connections (from_asset_id, to_asset_id, connection_type, direction).

Rules
  * Nothing is written until an admin confirms a stored preview.
  * Existing asset/connection IDs are never overwritten (reported as duplicates).
  * Connections must reference assets that exist in the file or the registry
    ("missing relationship" otherwise). Proximity never creates a connection.
  * Imported records are always `unverified`; verification is a separate act.
  * Provenance is kept per record; a web source can be at most
    `official_unverified` (mock TinyFish content is `synthetic_demo`).
"""
import csv
import hashlib
import io
import json
import re
import secrets
from datetime import timedelta

from config import SERVICE_RADIUS_KM
from database import query, db, now, parse_ts, utcnow
from services import geo
from services.utility_network import geometry, model
from services.utility_network.schema import PROVENANCE_CLASSES

MAX_RECORDS = 5000
MAX_CONTENT_CHARS = 6_000_000
PREVIEW_TTL_HOURS = 24
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{1,63}$")
_STATUS = set(model.OPERATIONAL_STATUSES)


class ImportError_(Exception):
    def __init__(self, message, status=422, extra=None):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra or {}


def _clean(v, n):
    if v is None:
        return None
    v = " ".join(str(v).split())[:n]
    return v or None


def _asset(props, geom, index, seen, existing, warnings):
    """Validate one asset; return (record, None) or (None, rejection)."""
    aid = _clean(props.get("asset_id") or props.get("id"), 64)
    ref = {"index": index, "id": aid}
    if not aid or not _ID_RE.match(aid):
        return None, ref | {"reason": "asset_id missing or invalid (letters, digits, . _ - ; 2–64 chars)"}
    if aid in seen:
        return None, ref | {"reason": "duplicate asset_id within this file", "kind": "duplicate"}
    if aid in existing:
        return None, ref | {"reason": "asset_id already exists in the registry — never overwritten", "kind": "duplicate"}
    ut, at = _clean(props.get("utility_type"), 20), _clean(props.get("asset_type"), 30)
    if ut not in model.UTILITY_TYPES:
        return None, ref | {"reason": f"unknown utility_type '{ut}'"}
    if at not in model.ASSET_TYPES:
        return None, ref | {"reason": f"unknown asset_type '{at}'"}
    try:
        gtype, gnorm, bbox = geometry.validate(geom)
    except geometry.GeometryError as e:
        return None, ref | {"reason": str(e)}
    if (gtype == "Point") != (at in model.POINT_TYPES):
        return None, ref | {"reason": f"asset_type '{at}' needs a {'Point' if at in model.POINT_TYPES else 'LineString'} geometry"}
    lat, lng = geometry.representative_point(gnorm)
    if not geo.in_service_area(lat, lng):
        hint = " (GeoJSON order is [longitude, latitude] — the coordinates may be swapped)" \
            if geo.in_service_area(lng, lat) else ""
        return None, ref | {"reason": f"outside the {SERVICE_RADIUS_KM:g} km service area{hint}"}
    status = _clean(props.get("operational_status"), 20)
    if status and status not in _STATUS:
        warnings.append(f"{aid}: unknown operational_status '{status}' ignored")
        status = None
    crit = _clean(props.get("criticality"), 10)
    if crit and crit not in model.CRITICALITIES:
        warnings.append(f"{aid}: unknown criticality '{crit}' ignored")
        crit = None
    attrs = {k: v for k, v in props.items() if k in model.ATTRIBUTE_KEYS and isinstance(v, (str, int, float))
             and not isinstance(v, bool) and str(v).strip() != ""}
    seen.add(aid)
    return {"asset_id": aid, "utility_type": ut, "asset_type": at, "subtype": _clean(props.get("subtype"), 40),
            "name": _clean(props.get("name"), 160) or aid, "geometry_type": gtype, "geometry": gnorm, "bbox": bbox,
            "operational_status": status, "operator_org": _clean(props.get("operator_org"), 120),
            "department": _clean(props.get("department"), 120), "criticality": crit,
            "attributes": {k: (str(v)[:80] if isinstance(v, str) else v) for k, v in attrs.items()},
            "source_record_id": _clean(props.get("source_record_id"), 80),
            "source_updated_at": _clean(props.get("source_updated_at"), 25)}, None


def _connection(c, index, file_ids, existing_ids, seen):
    ref = {"index": index}
    if not isinstance(c, dict):
        return None, ref | {"reason": "connection must be an object"}
    f, t = _clean(c.get("from_asset_id"), 64), _clean(c.get("to_asset_id"), 64)
    ref |= {"id": f"{f}->{t}"}
    if not f or not t:
        return None, ref | {"reason": "from_asset_id and to_asset_id are required"}
    if f == t:
        return None, ref | {"reason": "an asset cannot connect to itself"}
    missing = [x for x in (f, t) if x not in file_ids and x not in existing_ids]
    if missing:
        return None, ref | {"reason": f"missing relationship: {', '.join(missing)} not in this file or the registry",
                            "kind": "missing_relationship"}
    ctype = _clean(c.get("connection_type"), 20) or "feeds"
    direction = _clean(c.get("direction"), 20) or "unknown"
    if ctype not in model.CONNECTION_TYPES:
        return None, ref | {"reason": f"unknown connection_type '{ctype}'"}
    if direction not in model.DIRECTIONS:
        return None, ref | {"reason": f"direction must be one of {', '.join(model.DIRECTIONS)}"}
    key = (f, t, ctype)
    if key in seen:
        return None, ref | {"reason": "duplicate connection within this file", "kind": "duplicate"}
    seen.add(key)
    cid = _clean(c.get("connection_id"), 64) or f"C-{f}-{t}"[:64]
    return {"connection_id": cid, "from": f, "to": t, "connection_type": ctype, "direction": direction,
            "notes": _clean(c.get("notes"), 200)}, None


def _parse(input_format, content):
    if not isinstance(content, str) or not content.strip():
        raise ImportError_("The file is empty.")
    if len(content) > MAX_CONTENT_CHARS:
        raise ImportError_("The file is too large (max 6 MB).")
    if input_format == "geojson":
        try:
            doc = json.loads(content)
        except ValueError:
            raise ImportError_("The file is not valid JSON.") from None
        if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection" or not isinstance(doc.get("features"), list):
            raise ImportError_("Expected a GeoJSON FeatureCollection.")
        crs = (((doc.get("crs") or {}).get("properties") or {}).get("name") or "")
        if crs and not re.search(r"(CRS84|4326)$", crs):
            raise ImportError_(f"Unsupported CRS '{crs[:60]}'. Re-project to WGS 84 (EPSG:4326) first.")
        assets = [(f.get("properties") or {}, f.get("geometry")) if isinstance(f, dict) else ({}, None)
                  for f in doc["features"]]
        conns = doc.get("connections") or []
        if not isinstance(conns, list):
            raise ImportError_("'connections' must be a list.")
        return assets, conns
    if input_format == "csv":
        try:
            rows = list(csv.DictReader(io.StringIO(content)))
        except csv.Error:
            raise ImportError_("The CSV could not be read.") from None
        if not rows:
            raise ImportError_("The CSV has no data rows.")
        cols = {c.strip().lower() for c in rows[0].keys() if c}
        rows = [{(k or "").strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()} for r in rows]
        if {"from_asset_id", "to_asset_id"} <= cols:
            return [], rows
        if not {"asset_id", "utility_type", "asset_type", "lat", "lng"} <= cols:
            raise ImportError_("CSV needs asset columns (asset_id, utility_type, asset_type, name, lat, lng) "
                               "or connection columns (from_asset_id, to_asset_id, connection_type, direction).")
        assets = []
        for r in rows:
            try:
                geom = {"type": "Point", "coordinates": [float(r.get("lng")), float(r.get("lat"))]}
            except (TypeError, ValueError):
                geom = {"type": "Point", "coordinates": None}
            props = {k: v for k, v in r.items() if k not in ("lat", "lng")}
            for k in ("diameter_mm", "install_year", "depth_m", "voltage_kv", "length_m", "inspection_interval_days"):
                if props.get(k):
                    try:
                        props[k] = float(props[k]) if "." in props[k] else int(props[k])
                    except ValueError:
                        pass
            assets.append((props, geom))
        return assets, []
    raise ImportError_("format must be geojson or csv")


def source_from_snapshot(snapshot_id):
    s = query("SELECT * FROM source_snapshots WHERE id=?", (snapshot_id,), one=True)
    if not s:
        raise ImportError_("Source snapshot not found.", 404)
    if s["fetch_status"] != "ok" or s["review_status"] != "reviewed":
        raise ImportError_("Approve the fetched source on the Data sources page before importing from it.", 409)
    cand = query("""SELECT d.authority FROM source_candidates c JOIN source_discovery_runs d ON d.id=c.run_id
                    WHERE c.id=?""", (s["candidate_id"],), one=True) if s["candidate_id"] else None
    return {"name": s["title"] or s["url"], "source_type": "web_page" if s["method"] == "tinyfish_fetch"
            else "agent_extraction", "authority": cand["authority"] if cand else None, "source_url": s["url"],
            "license_notes": None, "snapshot_id": s["id"],
            "provenance_class": "synthetic_demo" if s["provider_mode"] == "mock" else "official_unverified",
            "is_synthetic": s["provider_mode"] == "mock"}


def preview(user, input_format, content, source):
    assets_in, conns_in = _parse(input_format, content)
    if len(assets_in) + len(conns_in) > MAX_RECORDS:
        raise ImportError_(f"At most {MAX_RECORDS} records per import.")
    prov = source.get("provenance_class")
    if prov not in PROVENANCE_CLASSES:
        raise ImportError_("Choose a provenance class from the agreed vocabulary.")
    existing = {r["asset_id"] for r in query("SELECT asset_id FROM utility_assets")}
    existing_conns = {(r["f"], r["t"], r["ct"]) for r in query(
        """SELECT fa.asset_id f, ta.asset_id t, c.connection_type ct FROM utility_connections c
           JOIN utility_assets fa ON fa.id=c.from_asset JOIN utility_assets ta ON ta.id=c.to_asset""")}
    seen, warnings, assets, a_rej = set(), [], [], []
    for i, (props, geom) in enumerate(assets_in):
        rec, rej = _asset(props if isinstance(props, dict) else {}, geom, i, seen, existing, warnings)
        (assets.append(rec) if rec else a_rej.append(rej))
    file_ids = {a["asset_id"] for a in assets}
    conns, c_rej, cseen = [], [], set()
    for i, c in enumerate(conns_in):
        rec, rej = _connection(c, i, file_ids, existing, cseen)
        if rec and (rec["from"], rec["to"], rec["connection_type"]) in existing_conns:
            rec, rej = None, {"index": i, "id": f"{rec['from']}->{rec['to']}",
                              "reason": "connection already recorded — never overwritten", "kind": "duplicate"}
        (conns.append(rec) if rec else c_rej.append(rej))
    isolated = [a["asset_id"] for a in assets if a["asset_id"] not in {c["from"] for c in conns} | {c["to"] for c in conns}]
    if conns_in and isolated:
        warnings.append(f"{len(isolated)} imported asset(s) have no recorded connection: {', '.join(isolated[:10])}")
    if assets and not conns_in:
        warnings.append("This file contains no connections. Assets will appear on the map but cannot be traced "
                        "until connections are imported; proximity is never used to connect them.")
    summary = {
        "assets": {"received": len(assets_in), "accepted": len(assets),
                   "duplicates": [r for r in a_rej if r.get("kind") == "duplicate"],
                   "rejected": [r for r in a_rej if r.get("kind") != "duplicate"]},
        "connections": {"received": len(conns_in), "accepted": len(conns),
                        "duplicates": [r for r in c_rej if r.get("kind") == "duplicate"],
                        "missing_relationships": [r for r in c_rej if r.get("kind") == "missing_relationship"],
                        "rejected": [r for r in c_rej if r.get("kind") not in ("duplicate", "missing_relationship")]},
        "warnings": warnings[:50],
        "verification_note": "Imported records are stored as unverified until an administrator verifies them.",
    }
    payload = json.dumps({"assets": assets, "connections": conns}, sort_keys=True)
    job_code = "IMP-" + secrets.token_hex(5).upper()
    with db() as conn:
        conn.execute("""INSERT INTO utility_import_jobs (job_code, status, input_format, source_meta, payload,
                        payload_sha256, summary, created_by, created_at) VALUES (?, 'previewed', ?,?,?,?,?,?,?)""",
                     (job_code, input_format, json.dumps(source), payload, hashlib.sha256(payload.encode()).hexdigest(),
                      json.dumps(summary), user["id"], now()))
    return {"job_code": job_code, "status": "previewed", "summary": summary, "source": source,
            "sample": [{k: a[k] for k in ("asset_id", "utility_type", "asset_type", "name", "geometry_type")}
                       for a in assets[:25]],
            "can_confirm": bool(assets or conns)}


def confirm(user, job_code):
    with db(immediate=True) as conn:
        job = conn.execute("SELECT * FROM utility_import_jobs WHERE job_code=?", (job_code,)).fetchone()
        if not job:
            raise ImportError_("Import preview not found.", 404)
        if job["status"] == "committed":
            raise ImportError_("This import was already committed.", 409, {"summary": json.loads(job["summary"])})
        if job["status"] != "previewed":
            raise ImportError_(f"This import is {job['status']} and cannot be committed.", 409)
        created = parse_ts(job["created_at"])
        if created and utcnow() - created > timedelta(hours=PREVIEW_TTL_HOURS):
            conn.execute("UPDATE utility_import_jobs SET status='expired' WHERE id=?", (job["id"],))
            raise ImportError_("This preview has expired. Run the preview again.", 409)
        data, src = json.loads(job["payload"]), json.loads(job["source_meta"])
        ts = now()
        cur = conn.execute("""INSERT INTO utility_data_sources (source_code, name, source_type, authority, source_url,
                              file_name, license_notes, crs, provenance_class, snapshot_id, is_synthetic, reviewed_by,
                              reviewed_at, created_at) VALUES (?,?,?,?,?,?,?, 'EPSG:4326', ?,?,?,?,?,?)""",
                           ("SRC-" + job_code, src.get("name") or job_code, src.get("source_type") or "file_import",
                            src.get("authority"), src.get("source_url"), src.get("file_name"), src.get("license_notes"),
                            src["provenance_class"], src.get("snapshot_id"), 1 if src.get("is_synthetic") else 0,
                            user["id"], ts, ts))
        source_id = cur.lastrowid
        synthetic = 1 if src.get("is_synthetic") or src["provenance_class"] == "synthetic_demo" else 0
        inserted, skipped = 0, []
        for a in data["assets"]:
            if conn.execute("SELECT 1 FROM utility_assets WHERE asset_id=?", (a["asset_id"],)).fetchone():
                skipped.append(a["asset_id"])  # appeared since the preview — still never overwritten
                continue
            min_lat, min_lng, max_lat, max_lng = a["bbox"]
            conn.execute("""INSERT INTO utility_assets (asset_id, utility_type, asset_type, subtype, name, geometry_type,
                            geometry, min_lat, min_lng, max_lat, max_lng, operational_status, operator_org, department,
                            criticality, attributes, is_control_device, is_source, sensitivity, source_id,
                            source_record_id, provenance_class, verification_status, is_synthetic, import_job_id,
                            source_updated_at, created_at, updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'unverified', ?,?,?,?,?)""",
                         (a["asset_id"], a["utility_type"], a["asset_type"], a["subtype"], a["name"], a["geometry_type"],
                          json.dumps(a["geometry"]), min_lat, min_lng, max_lat, max_lng, a["operational_status"],
                          a["operator_org"], a["department"], a["criticality"], json.dumps(a["attributes"]),
                          int(model.is_control(a["utility_type"], a["asset_type"])),
                          int(model.is_source(a["utility_type"], a["asset_type"])),
                          "restricted" if a["utility_type"] in ("gas", "electric") else "internal", source_id,
                          a["source_record_id"], src["provenance_class"], synthetic, job["id"], a["source_updated_at"],
                          ts, ts))
            inserted += 1
        c_inserted, c_skipped = 0, []
        for c in data["connections"]:
            f = conn.execute("SELECT id FROM utility_assets WHERE asset_id=?", (c["from"],)).fetchone()
            t = conn.execute("SELECT id FROM utility_assets WHERE asset_id=?", (c["to"],)).fetchone()
            if not f or not t:
                c_skipped.append(c["connection_id"])
                continue
            cid = c["connection_id"]
            if conn.execute("SELECT 1 FROM utility_connections WHERE connection_id=?", (cid,)).fetchone():
                cid = f"{cid}-{job_code}"[:64]
            cur = conn.execute("""INSERT OR IGNORE INTO utility_connections (connection_id, from_asset, to_asset,
                                  connection_type, direction, source_id, provenance_class, verification_status,
                                  is_synthetic, notes, import_job_id, created_at)
                                  VALUES (?,?,?,?,?,?,?, 'unverified', ?,?,?,?)""",
                               (cid, f["id"], t["id"], c["connection_type"], c["direction"], source_id,
                                src["provenance_class"], synthetic, c["notes"], job["id"], ts))
            if cur.rowcount:
                c_inserted += 1
            else:
                c_skipped.append(c["connection_id"])
        summary = json.loads(job["summary"]) | {"committed": {"assets": inserted, "connections": c_inserted,
                                                              "skipped_assets": skipped, "skipped_connections": c_skipped}}
        conn.execute("""UPDATE utility_import_jobs SET status='committed', committed_by=?, committed_at=?,
                        data_source_id=?, summary=?, payload=NULL WHERE id=?""",
                     (user["id"], ts, source_id, json.dumps(summary), job["id"]))
        conn.execute("INSERT INTO activity (actor, actor_id, action, detail, created_at) VALUES (?,?,?,?,?)",
                     (user["name"], user["id"], "utility_import_committed",
                      f"{job_code}: {inserted} asset(s), {c_inserted} connection(s) imported as unverified", ts))
    return {"job_code": job_code, "status": "committed", "data_source_id": source_id, "summary": summary}


def rollback(user, job_code):
    """Delete exactly the records a committed import created, if nothing depends on them yet.

    Blockers: confirmed incident links, maintenance records, work orders that name
    the assets, or connections recorded later by other imports/officers."""
    with db(immediate=True) as conn:
        job = conn.execute("SELECT * FROM utility_import_jobs WHERE job_code=?", (job_code,)).fetchone()
        if not job:
            raise ImportError_("Import not found.", 404)
        if job["status"] != "committed":
            raise ImportError_(f"Only committed imports can be rolled back (this one is {job['status']}).", 409)
        assets = conn.execute("SELECT id, asset_id FROM utility_assets WHERE import_job_id=?", (job["id"],)).fetchall()
        pks = [a["id"] for a in assets]
        codes = [a["asset_id"] for a in assets]
        blockers = []
        if pks:
            marks = ",".join("?" * len(pks))
            if conn.execute(f"SELECT 1 FROM incident_utility_assets WHERE status='confirmed' AND asset_id IN ({marks})",
                            pks).fetchone():
                blockers.append("an incident has a confirmed link to an imported asset")
            if conn.execute(f"SELECT 1 FROM utility_maintenance WHERE asset_id IN ({marks})", pks).fetchone():
                blockers.append("maintenance has been recorded on an imported asset")
            if conn.execute(f"""SELECT 1 FROM utility_connections WHERE (from_asset IN ({marks}) OR to_asset IN ({marks}))
                                AND (import_job_id IS NULL OR import_job_id != ?)""", (*pks, *pks, job["id"])).fetchone():
                blockers.append("other connections now reference an imported asset")
            for code in codes:
                if conn.execute("SELECT 1 FROM work_orders WHERE utility_asset_ids LIKE ?", (f'%"{code}"%',)).fetchone():
                    blockers.append(f"work orders reference {code}")
                    break
        if blockers:
            raise ImportError_("Rollback refused: " + "; ".join(blockers) + ". Correct the records instead.", 409,
                               {"blockers": blockers})
        c = conn.execute("DELETE FROM utility_connections WHERE import_job_id=?", (job["id"],)).rowcount
        if pks:
            conn.execute(f"DELETE FROM incident_utility_assets WHERE asset_id IN ({','.join('?' * len(pks))})", pks)
            conn.execute("DELETE FROM utility_assets WHERE import_job_id=?", (job["id"],))
        summary = json.loads(job["summary"]) | {"rolled_back": {"assets": len(pks), "connections": c, "at": now(),
                                                               "by": user["name"]}}
        conn.execute("UPDATE utility_import_jobs SET status='rolled_back', summary=? WHERE id=?",
                     (json.dumps(summary), job["id"]))
        if job["data_source_id"]:
            conn.execute("UPDATE utility_data_sources SET status='rolled_back' WHERE id=?", (job["data_source_id"],))
        conn.execute("INSERT INTO activity (actor, actor_id, action, detail, created_at) VALUES (?,?,?,?,?)",
                     (user["name"], user["id"], "utility_import_rolled_back",
                      f"{job_code}: {len(pks)} asset(s) and {c} connection(s) removed", now()))
    return {"job_code": job_code, "status": "rolled_back", "removed": {"assets": len(pks), "connections": c}}


def jobs(limit=50):
    rows = query("""SELECT j.job_code, j.status, j.input_format, j.source_meta, j.summary, j.created_at, j.committed_at,
                    u.name AS created_by_name FROM utility_import_jobs j LEFT JOIN users u ON u.id=j.created_by
                    ORDER BY j.id DESC LIMIT ?""", (limit,))
    for r in rows:
        r["source_meta"], r["summary"] = json.loads(r["source_meta"]), json.loads(r["summary"])
    return rows
