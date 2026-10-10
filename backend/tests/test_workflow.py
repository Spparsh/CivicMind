import os

from database import query
from services import evidence
from conftest import file_complaint, image_data_url, offset, PUNE


def _incident(make_user, category="Pothole", n=3, base=PUNE):
    citizens = [make_user("citizen") for _ in range(n)]
    last = None
    for i, c in enumerate(citizens):
        lat, lng = offset(*base, dlat_m=30 * i)
        last = file_complaint(c, category, lat, lng, text=f"Report {i}: pothole on the main road, vehicles swerving.")
        assert last.status_code == 200, last.text
    inc_id = last.json()["incident_id"]
    assert inc_id
    return citizens, inc_id


def _current_action(officer, inc_id):
    return officer.get(f"/api/incidents/{inc_id}").json()["current_action"]


def test_full_lifecycle(client, make_user):
    citizens, inc_id = _incident(make_user)
    off = make_user("officer")
    inc = off.get(f"/api/incidents/{inc_id}").json()
    assert inc["status"] == "active" and inc["report_count"] == 3
    assert inc["priority"]["factors"] and 0 <= inc["priority"]["score"] <= 100
    act = inc["current_action"]
    assert act["status"] == "pending" and act["source"] == "rules"

    assert off.post(f"/api/actions/{act['id']}/approve", json={"eta_hours": 12}).json()["status"] == "approved"
    assert off.get(f"/api/incidents/{inc_id}").json()["status"] == "in_progress"
    assert off.post(f"/api/actions/{act['id']}/start").json()["status"] == "in_progress"
    assert all(c["status"] == "in_progress" for c in citizens[0].get("/api/complaints").json())

    r = off.post(f"/api/actions/{act['id']}/complete", json={"notes": "Filled", "proof": image_data_url(77)})
    assert r.status_code == 200, r.text
    assert r.json()["incident_status"] == "resolved"
    final = off.get(f"/api/incidents/{inc_id}").json()
    assert final["status"] == "resolved"
    assert final["resolution"]["verification_type"] == "workflow_completion"
    assert "Not an independent field inspection" in final["resolution"]["summary"]
    assert final["resolution"]["officer_name"] == off.user["name"]
    for c in citizens:
        mine = c.get("/api/complaints").json()[0]
        assert mine["status"] == "resolved"
        titles = [n["title"] for n in c.get("/api/notifications").json()["items"]]
        assert "Issue resolved" in titles
    actors = {t["actor"] for t in final["timeline"]}
    assert off.user["name"] in actors and "Officer" not in actors  # real names, not a hard-coded label
    assert citizens[0].get(f"/api/actions/{act['id']}/proof").status_code == 200
    assert make_user("citizen").get(f"/api/actions/{act['id']}/proof").status_code == 404


def test_invalid_transitions_are_rejected(client, make_user):
    _, inc_id = _incident(make_user)
    off = make_user("officer")
    act = _current_action(off, inc_id)
    assert off.post(f"/api/actions/{act['id']}/start").status_code == 409          # not approved yet
    assert off.post(f"/api/actions/{act['id']}/complete", json={"proof": image_data_url(1)}).status_code == 409
    assert off.post(f"/api/actions/{act['id']}/approve").status_code == 200
    assert off.post(f"/api/actions/{act['id']}/approve").status_code == 409        # double approve
    assert off.post(f"/api/actions/{act['id']}/reject", json={"reason": "late"}).status_code == 409
    assert off.post("/api/actions/9999/approve").status_code == 404


def test_verify_cannot_resolve_without_proof(client, make_user):
    """Regression: the old /verify resolved an incident with no evidence at all."""
    _, inc_id = _incident(make_user)
    off = make_user("officer")
    r = off.post(f"/api/incidents/{inc_id}/verify")
    assert r.status_code == 409
    act = _current_action(off, inc_id)
    off.post(f"/api/actions/{act['id']}/approve")
    off.post(f"/api/actions/{act['id']}/start")
    assert off.post(f"/api/incidents/{inc_id}/verify").status_code == 409
    assert off.get(f"/api/incidents/{inc_id}").json()["status"] == "in_progress"


def test_completion_requires_valid_proof(client, make_user):
    _, inc_id = _incident(make_user)
    off = make_user("officer")
    act = _current_action(off, inc_id)
    off.post(f"/api/actions/{act['id']}/approve")
    off.post(f"/api/actions/{act['id']}/start")
    assert off.post(f"/api/actions/{act['id']}/complete", json={}).status_code == 422
    r = off.post(f"/api/actions/{act['id']}/complete", json={"proof": image_data_url(3, flat=True)})
    assert r.status_code == 422 and r.json()["detail"]["reasons"]
    assert _current_action(off, inc_id)["status"] == "in_progress"  # nothing changed


def test_proof_deleted_from_disk_blocks_reverification(client, make_user):
    _, inc_id = _incident(make_user)
    off = make_user("officer")
    act = _current_action(off, inc_id)
    off.post(f"/api/actions/{act['id']}/approve")
    off.post(f"/api/actions/{act['id']}/start")
    assert off.post(f"/api/actions/{act['id']}/complete", json={"proof": image_data_url(9)}).status_code == 200
    stored = query("SELECT proof_path FROM actions WHERE id=?", (act["id"],), one=True)["proof_path"]
    os.remove(evidence.resolve_path(stored))
    assert off.post(f"/api/incidents/{inc_id}/verify").status_code == 409


def test_reject_then_regenerate_plan(client, make_user):
    _, inc_id = _incident(make_user)
    off = make_user("officer")
    act = _current_action(off, inc_id)
    assert off.post(f"/api/incidents/{inc_id}/plans").status_code == 409  # one open plan at a time
    assert off.post(f"/api/actions/{act['id']}/reject", json={"reason": ""}).status_code == 422
    assert off.post(f"/api/actions/{act['id']}/reject", json={"reason": "Wrong team"}).status_code == 200
    detail = off.get(f"/api/incidents/{inc_id}").json()
    assert detail["can_generate_plan"] is True
    new = off.post(f"/api/incidents/{inc_id}/plans")
    assert new.status_code == 200 and new.json()["status"] == "pending"
    # the incident page must show the NEW plan, not the oldest one
    assert _current_action(off, inc_id)["id"] == new.json()["id"] != act["id"]


def test_reverify_reopens_on_new_related_reports(client, make_user):
    citizens, inc_id = _incident(make_user)
    off = make_user("officer")
    act = _current_action(off, inc_id)
    off.post(f"/api/actions/{act['id']}/approve")
    off.post(f"/api/actions/{act['id']}/start")
    off.post(f"/api/actions/{act['id']}/complete", json={"proof": image_data_url(11)})
    assert off.get(f"/api/incidents/{inc_id}").json()["status"] == "resolved"
    file_complaint(make_user("citizen"), "Pothole", *offset(*PUNE, dlat_m=10), text="Pothole came back on the main road.")
    r = off.post(f"/api/incidents/{inc_id}/verify")
    assert r.status_code == 200 and r.json()["status"] == "reopened"
    inc = off.get(f"/api/incidents/{inc_id}").json()
    assert inc["status"] == "reopened" and inc["report_count"] == 4 and inc["can_generate_plan"]


def test_direct_complaint_resolution(client, make_user):
    c = make_user("citizen")
    cid = file_complaint(c, image=image_data_url(21)).json()["id"]
    off = make_user("officer")
    # identical photo to the issue photo is refused
    assert off.post(f"/api/complaints/{cid}/resolve", json={"proof": image_data_url(21)}).status_code == 422
    r = off.post(f"/api/complaints/{cid}/resolve", json={"notes": "done", "proof": image_data_url(22)})
    assert r.status_code == 200
    assert off.post(f"/api/complaints/{cid}/resolve", json={"proof": image_data_url(23)}).status_code == 409
    mine = c.get(f"/api/complaints/{cid}").json()
    assert mine["status"] == "resolved" and mine["resolved_by_name"] == off.user["name"]
    assert c.get(f"/api/complaints/{cid}/resolution-photo").status_code == 200


def test_unrelated_categories_are_not_grouped(client, make_user):
    for i, cat in enumerate(["Pothole", "Streetlight Failure", "Garbage Accumulation"]):
        r = file_complaint(make_user("citizen"), cat, *offset(*PUNE, dlat_m=10 * i),
                           text=f"Issue {i} reported here today in the area.")
        assert r.json()["incident_id"] is None


def test_new_report_joins_existing_incident(client, make_user):
    _, inc_id = _incident(make_user)
    r = file_complaint(make_user("citizen"), "Road Damage", *offset(*PUNE, dlat_m=50),
                       text="Road surface is broken and cracked here.")
    assert r.json()["incident_id"] == inc_id
    assert query("SELECT COUNT(*) c FROM incidents", one=True)["c"] == 1


def test_complaint_validation(client, make_user):
    c = make_user("citizen")
    bad = [
        {"category": "Alien Invasion", "description": "something odd happened", "latitude": 18.52, "longitude": 73.85},
        {"category": "Pothole", "description": "short", "latitude": 18.52, "longitude": 73.85},
        {"category": "Pothole", "description": "valid description here", "latitude": 95, "longitude": 73.85},
        {"category": "Pothole", "description": "valid description here", "latitude": 28.61, "longitude": 77.2},
    ]
    for body in bad:
        assert c.post("/api/complaints", json=body).status_code == 422, body
    nan = '{"category":"Pothole","description":"valid description here","latitude":NaN,"longitude":73.85}'
    r = c.c.post("/api/complaints", content=nan, headers={**c.h, "Content-Type": "application/json"})
    assert r.status_code == 422


def test_duplicate_submission_blocked(client, make_user):
    c = make_user("citizen")
    assert file_complaint(c).status_code == 200
    r = file_complaint(c)
    assert r.status_code == 409 and r.json()["detail"]["public_id"]


def test_activity_records_actor_ids(client, make_user):
    _, inc_id = _incident(make_user)
    off = make_user("officer")
    act = _current_action(off, inc_id)
    off.post(f"/api/actions/{act['id']}/approve")
    row = query("SELECT actor, actor_id FROM activity WHERE action='action_approved'", one=True)
    assert row["actor_id"] == off.user["id"] and row["actor"] == off.user["name"]


def test_dashboard_metrics_are_consistent(client, make_user):
    off = make_user("officer")
    empty = off.get("/api/dashboard").json()["metrics"]
    assert empty["total_complaints"] == 0 and empty["resolution_rate"] is None
    charts = off.get("/api/dashboard/charts").json()
    assert len(charts["trend"]) == 7
    _incident(make_user)
    m = off.get("/api/dashboard").json()["metrics"]
    assert m["open_complaints"] == 3 and m["open_incidents"] == 1 and m["actions_awaiting_approval"] == 1
    assert m["reporting_citizens"] == 3 and m["resolution_rate"] == 0.0
