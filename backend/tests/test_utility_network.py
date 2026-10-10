"""Utility network intelligence: registry, deterministic tracing, candidates, findings, copilot, import, access."""
import json

import pytest

from database import db, query
from services import ai
from services.utility_network import demo_data, graph, registry
from conftest import file_complaint



@pytest.fixture()
def net(client):
    with db(immediate=True) as conn:
        first = demo_data.seed(conn)
    with db(immediate=True) as conn:
        second = demo_data.seed(conn)
    return first, second


def _leak_incident(make_user):
    citizens = [make_user("citizen") for _ in range(3)]
    last = None
    for i, c in enumerate(citizens):
        lat, lng = demo_data.LEAK_REPORTS[i][2:4]          # on the leak street, like the demo reports
        last = file_complaint(c, "Water Leakage", lat, lng,
                              text=f"Report {i}: water gushing from the road in Lane 7, the road is flooding.")
        assert last.status_code == 200, last.text
    assert last.json()["incident_id"]
    return citizens, last.json()["incident_id"]


# ------------------------------------------------------------------ registry + seed
def test_seed_is_deterministic_and_idempotent(net):
    first, second = net
    assert first["assets"] == len(demo_data.ASSETS) and first["connections"] == len(demo_data.CONNECTIONS)
    assert second == {"assets": 0, "connections": 0, "contacts": 0, "maintenance": 0}
    assert query("SELECT COUNT(*) c FROM utility_assets WHERE is_synthetic=0", one=True)["c"] == 0
    assert query("SELECT COUNT(*) c FROM utility_assets WHERE provenance_class NOT IN "
                 "('synthetic_demo','approximate_unverified')", one=True)["c"] == 0
    contacts = query("SELECT phone FROM utility_contacts")
    assert all("DEMO-ONLY" in c["phone"] for c in contacts)  # never a dialable number


def test_utility_routes_are_staff_only(client, make_user, net):
    citizen, officer = make_user("citizen"), make_user("officer")
    paths = ["/api/utility/meta", "/api/utility/assets", "/api/utility/assets/WM-DEMO-03",
             "/api/utility/assets/WM-DEMO-03/trace", "/api/utility/assets/WM-DEMO-03/contacts",
             "/api/utility/assets/WM-DEMO-03/isolation", "/api/utility/assets/WM-DEMO-03/history",
             "/api/utility/findings"]
    for p in paths:
        assert citizen.get(p).status_code == 403, p
        assert client.get(p).status_code == 401, p
        assert officer.get(p).status_code == 200, p
    assert citizen.post("/api/utility/assets/WM-DEMO-03/copilot", json={"question": "where does it connect"}).status_code == 403
    assert officer.post("/api/utility/import/preview", json={"format": "csv", "content": "a"}).status_code == 403
    assert officer.post("/api/utility/records/verify", json={"kind": "asset", "id": "V-DEMO-03",
                                                             "status": "verified", "note": "field check"}).status_code == 403


def test_asset_list_bbox_and_filters(client, make_user, net):
    off = make_user("officer")
    fc = off.get("/api/utility/assets", params={"utility_types": "water"}).json()
    assert fc["type"] == "FeatureCollection" and fc["features"]
    assert {f["properties"]["utility_type"] for f in fc["features"]} == {"water"}
    assert all("phone" not in json.dumps(f) for f in fc["features"])  # contacts never ride along on map data
    la, lo = demo_data.LEAK_POINT
    small = off.get("/api/utility/assets", params={"bbox": f"{lo - 0.0003},{la - 0.0003},{lo + 0.0003},{la + 0.0003}"}).json()
    assert "WM-DEMO-03" in {f["id"] for f in small["features"]}
    assert "SUB-DEMO-01" not in {f["id"] for f in small["features"]}
    for bad in ("1,2,3", "73.9,18.4,73.8,18.6", "70,10,75,20", "a,b,c,d"):
        assert off.get("/api/utility/assets", params={"bbox": bad}).status_code == 422, bad
    assert off.get("/api/utility/assets", params={"utility_types": "plasma"}).status_code == 422


def test_asset_detail_shows_records_not_guesses(client, make_user, net):
    d = make_user("officer").get("/api/utility/assets/WM-DEMO-03").json()
    assert d["attributes"] == {"diameter_mm": 200, "material": "cast iron", "install_year": 1994}
    assert d["data_source"]["provenance_class"] == "synthetic_demo" and d["is_synthetic"] is True
    rel = {n["asset"]["asset_id"]: n["relation"] for n in d["connections"]}
    assert rel == {"V-DEMO-02": "upstream", "J-DEMO-03": "downstream"}
    assert d["contacts"][0]["label"].startswith("DEMO ONLY") and d["contacts"][0]["operational_use"] is False
    j4 = make_user("officer").get("/api/utility/assets/J-DEMO-04").json()
    assert "No technical attributes" in " ".join(j4["notes"])
    assert make_user("officer").get("/api/utility/assets/NOPE-1").status_code == 404


def test_demo_network_follows_real_streets():
    """Every synthetic line runs along a street centre-line (OpenStreetMap extract), every
    utility type is present at city scale, and nothing is left unconnected."""
    from services.utility_network import street_network as sn
    streets = sn.Streets()
    centre = [s["xy"] for s in streets.segs]
    lines = [a for a in demo_data.ASSETS if a[4]["type"] == "LineString"]
    off_street = []
    for aid, _ut, _at, _name, geom, _extra in lines[::7]:     # a deterministic sample of ~1 in 7 lines
        for lng, lat in geom["coordinates"]:
            p = sn.to_xy(lat, lng)
            if min(sn.seg_distance(p, xy)[0] for xy in centre) > 12:   # wider roads hold utilities further out
                off_street.append(aid)
                break
    assert not off_street, off_street[:5]
    by_type = {}
    for a in demo_data.ASSETS:
        by_type[a[1]] = by_type.get(a[1], 0) + 1
    assert all(by_type.get(ut, 0) > 300 for ut in ("water", "sewer", "storm", "electric", "gas", "telecom")), by_type
    linked = {c[1] for c in demo_data.CONNECTIONS} | {c[2] for c in demo_data.CONNECTIONS}
    assert {a[0] for a in demo_data.ASSETS} <= linked
    assert "© OpenStreetMap contributors" in demo_data.STREETS_ATTRIBUTION


# ------------------------------------------------------------------ tracing
def test_upstream_trace_finds_source_and_nearest_valve(net):
    t = graph.trace("WM-DEMO-03", "upstream")
    assert [s["asset_id"] for s in t["possible_sources"]] == ["SRC-DEMO-01"]
    assert t["possible_sources"][0]["path"][0] == "WM-DEMO-03"
    cands = t["candidate_isolation_assets"]
    assert [c["asset_id"] for c in cands] == ["V-DEMO-02"]  # V-DEMO-01 lies behind V-DEMO-02
    assert cands[0]["path"] == ["WM-DEMO-03", "V-DEMO-02"] and cands[0]["path_verified"] is True
    assert cands[0]["status"].startswith("candidate")
    assert "UC-DEMO-05" in t["unverified_connections"]
    assert not t["dead_ends"]


def test_downstream_trace_reports_unknown_links_without_walking_them(net):
    t = graph.trace("WM-DEMO-03", "downstream")
    ids = {v["asset_id"] for v in t["visited_assets"]}
    assert {"J-DEMO-03", "V-DEMO-04", "WM-DEMO-07", "END-DEMO-01"} <= ids
    assert "WM-DEMO-06" not in ids and "J-DEMO-04" not in ids
    assert "UC-DEMO-13" in {u["connection_id"] for u in t["unknown_links"]}
    assert [c["asset_id"] for c in t["candidate_isolation_assets"]] == ["V-DEMO-04"]
    assert any("unknown flow direction" in w for w in t["warnings"])


def test_both_direction_trace_detects_the_loop(net):
    t = graph.trace("WM-DEMO-03", "both", max_hops=10)
    assert t["cycles_detected"]
    assert any("loop" in w for w in t["warnings"])
    assert "J-DEMO-04" in {v["asset_id"] for v in t["visited_assets"]}


def test_missing_link_is_reported_not_invented(net):
    # WM-DEMO-101 starts ~10 m from J-DEMO-03 on the map, but no connection is recorded.
    from services.utility_network import geometry
    a, j3 = registry.get_row("WM-DEMO-101"), registry.get_row("J-DEMO-03")
    lat, lng = geometry.representative_point(j3["geometry"])
    assert geometry.distance_m(lat, lng, a["geometry"]) < 20
    t = graph.trace("WM-DEMO-102", "upstream")
    assert not t["possible_sources"] and "WM-DEMO-101" in t["dead_ends"]
    assert any("No recorded source" in w for w in t["warnings"])
    assert "J-DEMO-03" not in {v["asset_id"] for v in graph.trace("WM-DEMO-101", "both")["visited_assets"]}


def test_hop_limit_and_validation(client, make_user, net):
    t = graph.trace("WM-DEMO-03", "upstream", max_hops=1)
    assert {v["asset_id"] for v in t["visited_assets"]} == {"WM-DEMO-03", "V-DEMO-02"}
    assert t["trace_limits"]["hop_limit_reached"] is True and not t["possible_sources"]
    off = make_user("officer")
    assert off.get("/api/utility/assets/WM-DEMO-03/trace", params={"direction": "sideways"}).status_code == 422
    assert off.get("/api/utility/assets/WM-DEMO-03/trace", params={"max_hops": 99}).status_code == 422
    with pytest.raises(graph.TraceError):
        graph.trace("WM-DEMO-03", "both", 0)


def test_semantics_differ_by_network(net):
    sewer = graph.trace("SWR-DEMO-01", "downstream")
    assert not sewer["candidate_isolation_assets"] and any("No isolation devices" in w for w in sewer["warnings"])
    assert "OUT-DEMO-01" in {v["asset_id"] for v in sewer["visited_assets"]}
    power = graph.isolation("CB-DEMO-02")
    assert [c["asset_id"] for c in power["upstream_candidates"]] == ["SW-DEMO-01"]
    assert [s["asset_id"] for s in power["sources"]] == ["SUB-DEMO-01"]
    fibre = graph.trace("FB-DEMO-01", "both")
    assert "CAB-DEMO-01" in {v["asset_id"] for v in fibre["visited_assets"]}


# ------------------------------------------------------------------ incidents, findings, copilot
def test_incident_candidates_findings_and_briefing(client, make_user, net):
    citizens, inc_id = _leak_incident(make_user)
    off = make_user("officer")
    ctx = off.get(f"/api/incidents/{inc_id}/utility-context").json()
    first = ctx["candidates"][0]
    assert first["asset_id"] == "WM-DEMO-03" and first["distance_m"] < 15
    assert "not evidence of cause" in first["reason"] and ctx["utility_types"] == ["water"]
    assert all(c["utility_type"] == "water" for c in ctx["candidates"])
    assert citizens[0].get(f"/api/incidents/{inc_id}/utility-context").status_code == 403

    r = off.post(f"/api/incidents/{inc_id}/utility-assets/WM-DEMO-03/review", json={"status": "confirmed"})
    assert r.status_code == 200
    assert {c["asset_id"]: c["link_status"] for c in r.json()["candidates"]}["WM-DEMO-03"] == "confirmed"
    hist = off.get("/api/utility/assets/WM-DEMO-03/history").json()
    assert hist["incidents"][0]["id"] == inc_id and len(hist["incidents"][0]["complaints"]) == 3
    assert hist["maintenance"][0]["record_id"] == "MR-DEMO-01"

    off.post("/api/utility/findings/scan")
    rules = {f["rule_id"]: f for f in off.get("/api/utility/findings").json()}
    assert {"U1", "U2", "U3"} <= set(rules)
    assert "MR-DEMO-01" in rules["U2"]["supporting"]["maintenance_ids"]
    assert len(rules["U1"]["supporting"]["complaint_ids"]) == 3
    if "U5" in rules:
        assert rules["U5"]["asset_ids"] == ["WM-DEMO-101"]

    b = off.post(f"/api/incidents/{inc_id}/briefing").json()
    assert b["engine"] == "records" and b["hypotheses"] and "not a confirmed cause" in b["hypotheses"][0]
    assert "V-DEMO-02" in b["citations"]
    steps = [s["step"] for s in b["checklist"]["steps"]]
    assert "Ask the operator to confirm the isolation point" in steps
    assert "does not operate valves" in b["checklist"]["note"]
    assert any("No verified operator contact" in g for g in b["data_gaps"])  # demo contact is fictional


def test_copilot_answers_from_records(client, make_user, net):
    off = make_user("officer")
    ask = lambda q: off.post("/api/utility/assets/WM-DEMO-03/copilot", json={"question": q}).json()  # noqa: E731
    iso = ask("Which valves could isolate this pipe?")
    assert iso["intent"] == "isolation" and iso["engine"] == "records"
    assert {"V-DEMO-02", "V-DEMO-04"} <= set(iso["citations"]) and "V-DEMO-01" not in iso["answer"]
    src = ask("What is the recorded upstream source?")
    assert "SRC-DEMO-01" in src["answer"]
    who = ask("Who is the operator?")
    assert "DEMO ONLY" in who["answer"]
    assert any("No verified operator contact" in g for g in who["data_gaps"])  # fictional contacts never count
    gap = off.post("/api/utility/assets/WM-DEMO-102/copilot", json={"question": "where does the supply come from"}).json()
    assert any("No recorded source" in g or "stops at" in g for g in gap["data_gaps"])


def test_copilot_rejects_ai_text_that_invents_records(client, make_user, net, monkeypatch):
    monkeypatch.setattr(ai, "ai_available", lambda: True)
    monkeypatch.setattr(ai, "_chat", lambda *a, **k: json.dumps({"answer": "Close V-DEMO-99 immediately."}))
    off = make_user("officer")
    r = off.post("/api/utility/assets/WM-DEMO-03/copilot", json={"question": "which valve upstream"}).json()
    assert r["engine"] == "records" and "V-DEMO-99" not in r["answer"]
    monkeypatch.setattr(ai, "_chat", lambda *a, **k: json.dumps(
        {"answer": "The nearest recorded upstream candidate is V-DEMO-02; the operator must confirm it."}))
    r = off.post("/api/utility/assets/WM-DEMO-03/copilot", json={"question": "which valve upstream"}).json()
    assert r["engine"] == "sarvam_grounded" and "V-DEMO-02" in r["answer"]


# ------------------------------------------------------------------ import + verification
GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[73.8200, 18.5100], [73.8210, 18.5105]]},
         "properties": {"asset_id": "IMP-WM-1", "utility_type": "water", "asset_type": "main", "name": "Imported main",
                        "diameter_mm": 150, "colour": "blue"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [73.8210, 18.5105]},
         "properties": {"asset_id": "IMP-V-1", "utility_type": "water", "asset_type": "valve"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [73.8100, 18.5000]},
         "properties": {"asset_id": "WM-DEMO-01", "utility_type": "water", "asset_type": "junction"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [18.5100, 73.8200]},
         "properties": {"asset_id": "IMP-SWAP", "utility_type": "water", "asset_type": "valve"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [73.82, 18.51]},
         "properties": {"asset_id": "IMP-BAD", "utility_type": "water", "asset_type": "main"}},
    ],
    "connections": [
        {"from_asset_id": "IMP-WM-1", "to_asset_id": "IMP-V-1", "connection_type": "feeds", "direction": "downstream"},
        {"from_asset_id": "IMP-V-1", "to_asset_id": "GHOST-9", "direction": "downstream"},
        {"from_asset_id": "IMP-V-1", "to_asset_id": "IMP-V-1"},
    ],
}


def test_import_preview_then_confirm(client, make_user, net):
    admin = make_user("admin")
    before = query("SELECT COUNT(*) c FROM utility_assets", one=True)["c"]
    r = admin.post("/api/utility/import/preview", json={
        "format": "geojson", "content": json.dumps(GEOJSON),
        "source": {"name": "Test ward GIS export", "provenance_class": "official_unverified"}})
    assert r.status_code == 200, r.text
    p = r.json()
    s = p["summary"]
    assert s["assets"]["accepted"] == 2
    assert [d["id"] for d in s["assets"]["duplicates"]] == ["WM-DEMO-01"]
    reasons = " ".join(x["reason"] for x in s["assets"]["rejected"])
    assert "swapped" in reasons and "LineString" in reasons
    assert s["connections"]["accepted"] == 1 and len(s["connections"]["missing_relationships"]) == 1
    assert query("SELECT COUNT(*) c FROM utility_assets", one=True)["c"] == before  # preview writes nothing

    c = admin.post("/api/utility/import/confirm", json={"job_code": p["job_code"]})
    assert c.status_code == 200, c.text
    assert c.json()["summary"]["committed"] == {"assets": 2, "connections": 1, "skipped_assets": [],
                                                 "skipped_connections": []}
    row = registry.get_row("IMP-WM-1")
    assert row["verification_status"] == "unverified" and row["provenance_class"] == "official_unverified"
    assert json.loads(row["attributes"]) == {"diameter_mm": 150}  # unknown keys dropped, nothing invented
    assert registry.get_row("WM-DEMO-01")["asset_type"] == "main"  # existing record never overwritten
    assert graph.isolation("IMP-WM-1")["downstream_candidates"][0]["asset_id"] == "IMP-V-1"
    again = admin.post("/api/utility/import/confirm", json={"job_code": p["job_code"]})
    assert again.status_code == 409
    assert admin.get("/api/utility/import/jobs").json()[0]["status"] == "committed"


def test_import_validation_errors(client, make_user, net):
    admin = make_user("admin")
    bad = admin.post("/api/utility/import/preview", json={"format": "geojson", "content": "{not json",
                                                          "source": {"name": "x"}})
    assert bad.status_code == 422
    crs = dict(GEOJSON, crs={"type": "name", "properties": {"name": "EPSG:32643"}})
    r = admin.post("/api/utility/import/preview", json={"format": "geojson", "content": json.dumps(crs),
                                                        "source": {"name": "UTM file"}})
    assert r.status_code == 422 and "EPSG:4326" in r.json()["detail"]["message"]
    r = admin.post("/api/utility/import/preview", json={"format": "geojson", "content": json.dumps(GEOJSON)})
    assert r.status_code == 422  # a source description is required
    csv_text = "from_asset_id,to_asset_id,connection_type,direction\nWM-DEMO-07,V-DEMO-04,feeds,downstream\n"
    r = admin.post("/api/utility/import/preview", json={"format": "csv", "content": csv_text,
                                                        "source": {"name": "Connections CSV"}})
    assert r.status_code == 200 and r.json()["summary"]["connections"]["accepted"] == 1
    r = admin.post("/api/utility/import/preview", json={"format": "geojson", "content": json.dumps(GEOJSON),
                                                        "snapshot_id": 999})
    assert r.status_code == 404


def test_import_from_unreviewed_snapshot_is_refused(client, make_user, net):
    admin = make_user("admin")
    with db() as conn:
        sid = conn.execute("""INSERT INTO source_snapshots (method, url, fetch_status, provider_mode, review_status)
                              VALUES ('tinyfish_fetch', 'https://example.org/x', 'ok', 'mock', 'pending_review')""").lastrowid
    body = {"format": "geojson", "content": json.dumps(GEOJSON), "snapshot_id": sid}
    assert admin.post("/api/utility/import/preview", json=body).status_code == 409
    with db() as conn:
        conn.execute("UPDATE source_snapshots SET review_status='reviewed' WHERE id=?", (sid,))
    p = admin.post("/api/utility/import/preview", json=body).json()
    assert p["source"]["provenance_class"] == "synthetic_demo"  # mock content never becomes official


def test_admin_verification_changes_candidate_status(client, make_user, net):
    admin = make_user("admin")
    assert graph.isolation("WM-DEMO-04")["downstream_candidates"][0]["path_verified"] is False  # V-DEMO-03 unverified
    r = admin.post("/api/utility/records/verify", json={"kind": "asset", "id": "V-DEMO-03", "status": "verified",
                                                        "note": "Confirmed by operator site visit"})
    assert r.status_code == 200
    assert graph.isolation("WM-DEMO-04")["downstream_candidates"][0]["path_verified"] is True
    assert query("SELECT 1 FROM activity WHERE action='utility_record_verification'", one=True)
    assert admin.post("/api/utility/records/verify", json={"kind": "asset", "id": "NOPE", "status": "verified",
                                                           "note": "x" * 6}).status_code == 404
