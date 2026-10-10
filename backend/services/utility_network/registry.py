"""CivicMind — utility registry reads: map features, asset detail, neighbours, contacts, history.

Views are purpose-built: no raw rows leave this module. Unknown attributes
are returned as absent, never guessed."""
import json

from database import query
from services.utility_network import geometry, model

ASSET_COLS = """a.id, a.asset_id, a.utility_type, a.asset_type, a.subtype, a.name, a.geometry_type, a.geometry,
                a.operational_status, a.operator_org, a.department, a.criticality, a.attributes,
                a.is_control_device, a.is_source, a.sensitivity, a.source_id, a.source_record_id,
                a.provenance_class, a.verification_status, a.is_synthetic, a.source_updated_at,
                a.last_verified_at, a.updated_at"""


def _json(val, default=None):
    if not val:
        return default
    try:
        return json.loads(val)
    except (TypeError, ValueError):
        return default


def get_row(asset_id):
    if not isinstance(asset_id, str) or not 1 <= len(asset_id) <= 64:
        return None
    return query(f"SELECT {ASSET_COLS} FROM utility_assets a WHERE a.asset_id=?", (asset_id,), one=True)


def get_row_by_pk(pk):
    return query(f"SELECT {ASSET_COLS} FROM utility_assets a WHERE a.id=?", (pk,), one=True)


def summary(a):
    """Compact identity used in lists, traces and citations."""
    lat, lng = geometry.representative_point(a["geometry"])
    return {"asset_id": a["asset_id"], "name": a["name"], "utility_type": a["utility_type"],
            "asset_type": a["asset_type"], "is_control_device": bool(a["is_control_device"]),
            "is_source": bool(a["is_source"]), "verification_status": a["verification_status"],
            "provenance_class": a["provenance_class"], "is_synthetic": bool(a["is_synthetic"]),
            "operational_status": a["operational_status"], "lat": round(lat, 6), "lng": round(lng, 6)}


def feature(a):
    props = summary(a)
    props.update({"criticality": a["criticality"], "operator_org": a["operator_org"],
                  "geometry_type": a["geometry_type"]})
    return {"type": "Feature", "id": a["asset_id"], "geometry": json.loads(a["geometry"]), "properties": props}


def list_features(bbox=None, utility_types=None, status=None, provenance=None, verification=None, limit=2000):
    sql, params = f"SELECT {ASSET_COLS} FROM utility_assets a WHERE 1=1", []
    if bbox:
        min_lng, min_lat, max_lng, max_lat = bbox
        sql += " AND a.max_lat >= ? AND a.min_lat <= ? AND a.max_lng >= ? AND a.min_lng <= ?"
        params += [min_lat, max_lat, min_lng, max_lng]
    if utility_types:
        sql += f" AND a.utility_type IN ({','.join('?' * len(utility_types))})"
        params += list(utility_types)
    for col, val in (("operational_status", status), ("provenance_class", provenance),
                     ("verification_status", verification)):
        if val:
            sql += f" AND a.{col}=?"
            params.append(val)
    rows = query(sql + " ORDER BY a.utility_type, a.asset_id LIMIT ?", tuple(params + [limit + 1]))
    truncated = len(rows) > limit
    return {"type": "FeatureCollection", "features": [feature(r) for r in rows[:limit]],
            "truncated": truncated, "limit": limit}


def data_source(source_id):
    if not source_id:
        return None
    s = query("""SELECT id, source_code, name, source_type, authority, source_url, license_notes, crs,
                 provenance_class, is_synthetic, created_at FROM utility_data_sources WHERE id=?""",
              (source_id,), one=True)
    if s:
        s["is_synthetic"] = bool(s["is_synthetic"])
        s["provenance_label"] = model.PROVENANCE_LABELS.get(s["provenance_class"], s["provenance_class"])
    return s


def edge_view(c):
    return {"connection_id": c["connection_id"], "from": c["from_code"], "to": c["to_code"],
            "connection_type": c["connection_type"], "direction": c["direction"],
            "verification_status": c["verification_status"], "provenance_class": c["provenance_class"],
            "is_synthetic": bool(c["is_synthetic"]), "notes": c.get("notes")}


EDGE_SQL = """SELECT c.*, fa.asset_id AS from_code, ta.asset_id AS to_code
              FROM utility_connections c JOIN utility_assets fa ON fa.id=c.from_asset
              JOIN utility_assets ta ON ta.id=c.to_asset
              WHERE (c.valid_to IS NULL OR c.valid_to >= date('now'))"""


def edges_of(pk):
    return query(EDGE_SQL + " AND (c.from_asset=? OR c.to_asset=?)", (pk, pk))


def neighbours(a):
    """Directly recorded connections, described relative to this asset."""
    out = []
    for c in edges_of(a["id"]):
        outgoing = c["from_asset"] == a["id"]
        other = get_row_by_pk(c["to_asset"] if outgoing else c["from_asset"])
        if c["direction"] == "downstream":
            rel = "downstream" if outgoing else "upstream"
        else:
            rel = c["direction"]  # bidirectional | unknown
        out.append({"relation": rel, "asset": summary(other), "connection": edge_view(c)})
    order = {"upstream": 0, "downstream": 1, "bidirectional": 2, "unknown": 3}
    return sorted(out, key=lambda n: (order.get(n["relation"], 9), n["asset"]["asset_id"]))


def contacts_for(a):
    rows = query("""SELECT * FROM utility_contacts
                    WHERE asset_id=? OR (asset_id IS NULL AND utility_type=?
                          AND (operator_org IS NULL OR operator_org=?))
                    ORDER BY verification_status='verified' DESC, is_emergency DESC, id""",
                 (a["id"], a["utility_type"], a["operator_org"] or ""))
    out = []
    for r in rows:
        verified = r["verification_status"] == "verified"
        out.append({"contact_id": r["contact_id"], "organization": r["organization"], "role": r["role"],
                    "phone": r["phone"], "email": r["email"], "is_emergency": bool(r["is_emergency"]),
                    "hours": r["hours"], "verification_status": r["verification_status"],
                    "last_verified_at": r["last_verified_at"], "is_synthetic": bool(r["is_synthetic"]),
                    "source": (data_source(r["source_id"]) or {}).get("name"),
                    "operational_use": verified and not r["is_synthetic"],
                    "label": ("DEMO ONLY — fictional contact, not callable" if r["is_synthetic"] else
                              "Verified operator contact" if verified else
                              "Unverified — do not rely on it operationally")})
    return out


def history(a):
    maint = query("""SELECT record_id, work_type, performed_on, outcome, reference, department, evidence_ref,
                     provenance_class, is_synthetic FROM utility_maintenance WHERE asset_id=?
                     ORDER BY performed_on DESC LIMIT 50""", (a["id"],))
    incidents = query("""SELECT i.id, i.public_id, i.title, i.status, i.category, i.priority_label, i.created_at,
                         iua.status AS link_status, iua.distance_m, iua.reason
                         FROM incident_utility_assets iua JOIN incidents i ON i.id=iua.incident_id
                         WHERE iua.asset_id=? AND iua.status != 'rejected' ORDER BY i.created_at DESC LIMIT 50""",
                      (a["id"],))
    for i in incidents:
        i["complaints"] = query("""SELECT public_id, category, status, created_at FROM complaints
                                   WHERE incident_id=? ORDER BY created_at""", (i["id"],))
    return {"maintenance": [dict(m, is_synthetic=bool(m["is_synthetic"])) for m in maint], "incidents": incidents}


def detail(a):
    attrs = _json(a["attributes"], {}) or {}
    g = json.loads(a["geometry"])
    out = summary(a) | {
        "subtype": a["subtype"], "criticality": a["criticality"], "operator_org": a["operator_org"],
        "department": a["department"], "attributes": {k: v for k, v in attrs.items() if k in model.ATTRIBUTE_KEYS},
        "geometry": g, "geometry_type": a["geometry_type"],
        "length_m": round(geometry.length_m(g)) if a["geometry_type"] == "LineString" else None,
        "sensitivity": a["sensitivity"], "source_record_id": a["source_record_id"],
        "source_updated_at": a["source_updated_at"], "last_verified_at": a["last_verified_at"],
        "updated_at": a["updated_at"],
        "provenance_label": model.PROVENANCE_LABELS.get(a["provenance_class"], a["provenance_class"]),
        "utility_label": model.UTILITY_TYPES.get(a["utility_type"], {}).get("label", a["utility_type"]),
        "data_source": data_source(a["source_id"]),
        "connections": neighbours(a),
        "contacts": contacts_for(a),
        "isolation_applicable": model.has_isolation(a["utility_type"]),
        "notes": [],
    }
    if not attrs:
        out["notes"].append("No technical attributes (diameter, material, depth…) are recorded for this asset.")
    if a["verification_status"] != "verified":
        out["notes"].append("This asset record is not field-verified.")
    if a["provenance_class"] == "planned_design":
        out["notes"].append("Planned / design record — it may not be installed as drawn.")
    if not out["connections"]:
        out["notes"].append("No connections are recorded for this asset.")
    return out
