from datetime import timedelta

from database import query, db, now, utcnow
from services import assets as asset_svc, risk, priority
from conftest import file_complaint, offset, PUNE

SRC = "Test fixture (synthetic)"


def _asset(code, atype, dlat_m=0, crit="medium", **kw):
    lat, lng = offset(*PUNE, dlat_m=dlat_m)
    return asset_svc.create_asset({"asset_code": code, "asset_type": atype, "latitude": lat, "longitude": lng,
                                   "criticality": crit, "data_source": SRC, "is_synthetic": True, **kw})


def test_asset_validation(client):
    good = {"asset_code": "SL-1", "asset_type": "streetlight", "latitude": PUNE[0], "longitude": PUNE[1],
            "data_source": SRC}
    assert _asset("SL-1", "streetlight")
    bad = [{**good, "asset_code": "SL-1"},                     # duplicate
           {**good, "asset_code": "x y"},                      # bad code
           {**good, "asset_code": "SL-2", "asset_type": "ufo"},
           {**good, "asset_code": "SL-3", "latitude": float("nan")},
           {**good, "asset_code": "SL-4", "latitude": 28.6, "longitude": 77.2},
           {**good, "asset_code": "SL-5", "criticality": "extreme"},
           {**good, "asset_code": "SL-6", "data_source": ""},
           {**good, "asset_code": "SL-7", "last_inspected_at": "2999-01-01"}]
    for b in bad:
        try:
            asset_svc.create_asset(b)
            raise AssertionError(f"accepted {b}")
        except asset_svc.AssetError:
            pass


def test_matching_prefers_compatible_type_over_nearest(client, make_user):
    _asset("RD-1", "road_segment", dlat_m=5)        # nearest, but wrong type
    _asset("SL-1", "streetlight", dlat_m=60)
    r = file_complaint(make_user("citizen"), "Streetlight Failure", *offset(*PUNE),
                       text="The street light is not working at night.").json()
    assert r["asset_match"]["asset_code"] == "SL-1" and 50 < r["asset_match"]["distance_m"] < 70


def test_no_match_for_unrelated_category_or_far_asset(client, make_user):
    _asset("SL-1", "streetlight")
    r = file_complaint(make_user("citizen"), "Encroachment", text="Hawkers occupying the footpath here.").json()
    assert r["asset_match"] is None
    _asset("SL-FAR", "streetlight", dlat_m=900)
    r = file_complaint(make_user("citizen"), "Streetlight Failure", *offset(*PUNE, dlat_m=800),
                       text="Dark street, lamp broken").json()
    assert r["asset_match"] is None or r["asset_match"]["asset_code"] == "SL-FAR"
    r = file_complaint(make_user("citizen"), "Streetlight Failure", *offset(*PUNE, dlat_m=400),
                       text="Another streetlight problem midway").json()
    assert r["asset_match"] is None  # 400 m from both assets > 150 m radius


def test_rules_fire_from_real_records(client, make_user):
    _asset("SL-14", "streetlight", crit="critical", criticality_reason="school crossing",
           inspection_interval_days=5, last_inspected_at="2020-01-01")
    aid = query("SELECT id FROM assets WHERE asset_code='SL-14'", one=True)["id"]
    ten_days_ago = (utcnow() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        asset_svc.add_maintenance(conn, aid, asset_svc.validate_maintenance(
            {"event_type": "repair", "performed_at": ten_days_ago, "data_source": SRC}))
    # the repair moved last_inspected_at to 10 days ago — still overdue for a 5-day interval
    for i in range(3):
        file_complaint(make_user("citizen"), "Streetlight Failure", *offset(*PUNE, dlat_m=10 * i),
                       text=f"Streetlight {i} is dark again near the school children crossing.")
    risk.scan()
    rules = {r["rule_id"]: r for r in query("SELECT * FROM risk_findings WHERE status='open'")}
    assert {"A", "B", "C", "E"} <= set(rules)
    assert "3 open complaints" in rules["A"]["explanation"] and "SL-14" in rules["A"]["explanation"]
    assert "not proof" in rules["B"]["explanation"]
    assert "10 day(s) overdue" not in rules["E"]["explanation"] and "5 days" in rules["E"]["explanation"]
    assert rules["C"]["severity"] == "critical"
    assert rules["A"]["is_synthetic"] == 1


def test_rules_do_not_fire_without_data(client, make_user):
    _asset("SL-1", "streetlight")  # no inspection data, no maintenance
    file_complaint(make_user("citizen"), "Streetlight Failure", text="One streetlight is out tonight.")
    risk.scan()
    assert query("SELECT COUNT(*) c FROM risk_findings", one=True)["c"] == 0


def test_scan_is_idempotent_and_clears(client, make_user):
    _asset("SL-1", "streetlight")
    cs = [file_complaint(make_user("citizen"), "Streetlight Failure", *offset(*PUNE, dlat_m=10 * i),
                         text=f"Light {i} is not working on our street.").json() for i in range(3)]
    risk.scan()
    risk.scan()
    assert query("SELECT COUNT(*) c FROM risk_findings WHERE rule_id='A'", one=True)["c"] == 1
    with db() as conn:
        conn.execute("UPDATE complaints SET status='resolved' WHERE id=?", (cs[0]["id"],))
    risk.scan()
    assert query("SELECT status FROM risk_findings WHERE rule_id='A'", one=True)["status"] == "cleared"


def test_conflicting_actions_only_across_incidents(client, make_user):
    _asset("SL-1", "streetlight")
    for i in range(3):
        file_complaint(make_user("citizen"), "Streetlight Failure", *offset(*PUNE, dlat_m=10 * i),
                       text=f"Light {i} is out on this road.")
    risk.scan()
    assert not query("SELECT 1 FROM risk_findings WHERE rule_id='F'", one=True)
    # second incident on the same asset (backdated so it is outside the grouping window)
    old = "2026-01-01 10:00:00"
    with db() as conn:
        aid = conn.execute("SELECT id FROM assets").fetchone()["id"]
        conn.execute("""INSERT INTO incidents (public_id, title, type, category, severity, status, centroid_lat,
                        centroid_lng, report_count, created_at) VALUES ('INC-T2','t','Streetlight Failure',
                        'Streetlight Failure',2,'active',?,?,1,?)""", (PUNE[0], PUNE[1], old))
        iid = conn.execute("SELECT id FROM incidents WHERE public_id='INC-T2'").fetchone()["id"]
        cur = conn.execute("""INSERT INTO complaints (public_id, user_id, category, description, latitude, longitude,
                              status, incident_id, created_at) VALUES ('CMP-T2',1,'Streetlight Failure','x',?,?,
                              'clustered',?,?)""", (PUNE[0], PUNE[1], iid, old))
        conn.execute("INSERT INTO incident_complaints VALUES (?,?)", (iid, cur.lastrowid))
        conn.execute("INSERT INTO complaint_assets (complaint_id, asset_id, method) VALUES (?,?, 'test')",
                     (cur.lastrowid, aid))
        conn.execute("INSERT INTO actions (incident_id, action_text, status) VALUES (?, 'fix', 'pending')", (iid,))
    risk.scan()
    f = query("SELECT * FROM risk_findings WHERE rule_id='F'", one=True)
    assert f and "different incidents" in f["explanation"]


def test_priority_is_bounded_and_explained():
    low = priority.compute_priority(1, 1, None, "Other")
    high = priority.compute_priority(5, 50, "2020-01-01 00:00:00", "Sewage Overflow", "DR-1", "critical")
    assert low["priority_score"] == 0 and low["priority_label"] == "Low"
    assert high["priority_score"] == 100 and high["priority_label"] == "Critical"
    assert sum(f["max"] for f in high["factors"]) == 100
    assert any("Critical asset" in r for r in high["reasons"])
    mid = priority.compute_priority(3, 3, None, "Streetlight Failure")
    assert not any("High reported severity" in r for r in mid["reasons"])


def test_asset_api_and_geojson_import(client, make_user):
    admin, off = make_user("admin"), make_user("officer")
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [PUNE[1], PUNE[0]]},
         "properties": {"asset_code": "SL-100", "asset_type": "streetlight", "criticality": "high"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [PUNE[1], PUNE[0]]},
         "properties": {"asset_code": "SL-100", "asset_type": "streetlight"}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[1, 2], [3, 4]]},
         "properties": {"asset_code": "RD-1", "asset_type": "road_segment"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [999, 999]},
         "properties": {"asset_code": "SL-101", "asset_type": "streetlight"}},
    ]}
    r = admin.post("/api/assets/import", json={"geojson": gj, "data_source": "Unit test", "is_synthetic": True})
    assert r.status_code == 200
    s = r.json()
    assert s["imported"] == 1 and s["duplicates"] == ["SL-100"] and len(s["rejected"]) == 2
    assert admin.post("/api/assets/import", json={"geojson": {"type": "Nope"}, "data_source": "x y"}).status_code == 422
    assets = off.get("/api/assets").json()
    assert assets[0]["asset_code"] == "SL-100" and assets[0]["is_synthetic"] == 1
    aid = assets[0]["id"]
    assert off.post(f"/api/assets/{aid}/maintenance", json={"event_type": "teleport", "performed_at": "2026-01-01"}).status_code == 422
    assert off.post(f"/api/assets/{aid}/maintenance", json={"event_type": "inspection", "performed_at": "2026-01-01"}).status_code == 200
    detail = off.get(f"/api/assets/{aid}").json()
    assert detail["maintenance"][0]["event_type"] == "inspection" and detail["last_inspected_at"].startswith("2026-01-01")


def test_match_review_and_completion_records_maintenance(client, make_user):
    _asset("SL-1", "streetlight")
    cs = [file_complaint(make_user("citizen"), "Streetlight Failure", *offset(*PUNE, dlat_m=10 * i),
                         text=f"Light {i} not working.").json() for i in range(3)]
    off = make_user("officer")
    inc = off.get(f"/api/incidents/{cs[-1]['incident_id']}").json()
    assert inc["asset"]["asset_code"] == "SL-1" and inc["asset"]["matched_reports"] == 3
    assert any(f["rule_id"] == "A" for f in inc["findings"])
    assert off.post(f"/api/complaints/{cs[0]['id']}/asset-match", json={"status": "rejected"}).status_code == 200
    assert off.get(f"/api/incidents/{inc['id']}").json()["asset"]["matched_reports"] == 2
    from conftest import image_data_url
    act = inc["current_action"]
    off.post(f"/api/actions/{act['id']}/approve")
    off.post(f"/api/actions/{act['id']}/start")
    assert off.post(f"/api/actions/{act['id']}/complete", json={"proof": image_data_url(55)}).status_code == 200
    m = query("SELECT * FROM asset_maintenance WHERE action_id=?", (act["id"],), one=True)
    assert m and m["event_type"] == "repair" and m["data_source"] == "CivicMind workflow"


def test_findings_review(client, make_user):
    _asset("SL-1", "streetlight", inspection_interval_days=10, last_inspected_at="2020-01-01")
    off = make_user("officer")
    off.post("/api/risk/scan")
    f = off.get("/api/risk/findings").json()[0]
    assert f["rule_id"] == "E" and f["rule_title"] == "Overdue inspection"
    assert off.post(f"/api/risk/findings/{f['id']}/review", json={"status": "dismissed", "note": "known"}).status_code == 200
    off.post("/api/risk/scan")
    assert query("SELECT status FROM risk_findings WHERE id=?", (f["id"],), one=True)["status"] == "dismissed"
    assert off.post(f"/api/risk/findings/{f['id']}/review", json={"status": "bogus"}).status_code == 422


def test_now_format_is_sqlite_compatible():
    assert len(now()) == 19 and now()[10] == " "


def test_repeat_after_maintenance_respects_lookback(client, make_user):
    _asset("SL-9", "streetlight")
    aid = query("SELECT id FROM assets WHERE asset_code='SL-9'", one=True)["id"]
    with db() as conn:  # far outside the 90-day lookback
        asset_svc.add_maintenance(conn, aid, asset_svc.validate_maintenance(
            {"event_type": "repair", "performed_at": "2025-01-01", "data_source": SRC}))
    file_complaint(make_user("citizen"), "Streetlight Failure", text="Streetlight is out again tonight.")
    risk.scan()
    assert not query("SELECT 1 FROM risk_findings WHERE rule_id='B'", one=True)
