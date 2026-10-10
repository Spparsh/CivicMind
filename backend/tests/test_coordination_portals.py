"""Portal routing, and operator coordination in the water-leak response workflow.

The utility network trace and operator contacts come from the Astra module
after the merge; these tests cover the workflow side that CivicMind owns."""
import re

import main
from conftest import file_complaint, image_data_url, offset, PUNE


# --------------------------------------------------------------------------- portals
def test_portal_entry_points_redirect(client):
    for path, target in {"/citizen": "/citizen.html", "/officer": "/dashboard.html", "/admin": "/admin.html",
                         "/login": "/index.html#login"}.items():
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == target, path
    assert client.get("/citizen.html").status_code == 200


def test_map_tiles_are_allowed(client):
    """OpenStreetMap refuses tile requests without a Referer, so the policy must send the origin."""
    r = client.get("/citizen.html")
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "https://tile.openstreetmap.org" in r.headers["content-security-policy"]


def test_login_home_comes_from_server_role(client, make_user):
    homes = {"citizen": "/citizen.html", "officer": "/dashboard.html", "admin": "/admin.html"}
    for role, home in homes.items():
        actor = make_user(role)
        assert actor.get("/api/auth/me").json()["home"] == home
        r = client.post("/api/auth/login", json={"email": actor.user["email"], "password": "password123"})
        assert r.json()["home"] == home
    # The admin data-source API stays closed to other roles regardless of which page they open.
    assert make_user("officer").get("/api/admin/data-sources/status").status_code == 403


# --------------------------------------------------------------------------- coordination
def _leak_incident(make_user):
    citizens = [make_user("citizen") for _ in range(3)]
    last = None
    for i, c in enumerate(citizens):
        lat, lng = offset(*PUNE, dlat_m=25 * i)
        last = file_complaint(c, "Water Leakage", lat, lng,
                              text=f"Report {i}: water pipe leaking heavily, road flooded near the bus stop.")
        assert last.status_code == 200, last.text
    assert last.json()["incident_id"]
    return citizens, last.json()["incident_id"]


def _op(officer, inc_id, **body):
    return officer.post(f"/api/incidents/{inc_id}/operator-updates", json=body)


def test_water_leak_response_sequence(client, make_user):
    citizens, inc_id = _leak_incident(make_user)
    off = make_user("officer")
    inc = off.get(f"/api/incidents/{inc_id}").json()
    assert inc["coordination"]["isolation_status"] == "not_recorded"
    assert "does not operate valves" in inc["coordination"]["safety_note"]

    # Deterministic prerequisites.
    r = _op(off, inc_id, step="isolation_confirmed_by_operator", operator_org="Water Supply Dept (DEMO)",
            reference="OPS-1")
    assert r.status_code == 409
    r = _op(off, inc_id, step="crew_dispatched")
    assert r.status_code == 409
    assert _op(off, inc_id, step="operator_contacted").status_code == 422  # operator organisation required
    assert _op(off, inc_id, step="valve_closed", operator_org="x").status_code == 422  # unknown step

    assert _op(off, inc_id, step="operator_contacted", operator_org="Water Supply Dept (DEMO)",
               contact_ref="DEMO-CONTACT-1").status_code == 200
    r = _op(off, inc_id, step="isolation_confirmed_by_operator", operator_org="Water Supply Dept (DEMO)")
    assert r.status_code == 422  # confirmation needs the operator's reference or a note
    r = _op(off, inc_id, step="isolation_confirmed_by_operator", operator_org="Water Supply Dept (DEMO)",
            reference="OPS-1", asset_ref="DEMO-VALVE-12", note="Operator closed the section valve on site")
    assert r.status_code == 200 and r.json()["isolation_status"] == "confirmed_by_operator"

    act = off.get(f"/api/incidents/{inc_id}").json()["current_action"]
    assert off.post(f"/api/actions/{act['id']}/approve", json={}).status_code == 200
    assert _op(off, inc_id, step="crew_dispatched", note="Repair crew B").status_code == 200
    assert off.post(f"/api/actions/{act['id']}/start").status_code == 200
    r = off.post(f"/api/actions/{act['id']}/complete", json={"notes": "Pipe joint replaced",
                                                            "proof": image_data_url(91)})
    assert r.status_code == 200 and r.json()["incident_status"] == "resolved"

    final = off.get(f"/api/incidents/{inc_id}").json()
    steps = [u["step"] for u in final["coordination"]["updates"]]
    assert steps == ["operator_contacted", "isolation_confirmed_by_operator", "crew_dispatched"]
    assert all(u["recorded_by_name"] == off.user["name"] for u in final["coordination"]["updates"])
    assert final["resolution"]["status"] == "verified"
    assert _op(off, inc_id, step="operator_contacted", operator_org="x").status_code == 409  # closed

    # Citizens see the dispatch, not internal isolation details.
    cit = citizens[0].get(f"/api/incidents/{inc_id}").json()
    actions = [t["action"] for t in cit["timeline"]]
    assert "Repair crew dispatched" in actions
    assert not any("solation" in a for a in actions) and "coordination" not in cit


def test_coordination_is_staff_only(client, make_user):
    citizens, inc_id = _leak_incident(make_user)
    assert citizens[0].get(f"/api/incidents/{inc_id}/operator-updates").status_code == 403
    assert _op(citizens[0], inc_id, step="operator_contacted", operator_org="x").status_code == 403
    assert make_user("officer").get("/api/incidents/999999/operator-updates").status_code == 404


def test_audit_log_is_admin_only(client, make_user):
    admin = make_user("admin")
    admin.post("/api/admin/users", json={"name": "New Officer", "email": "new.officer@example.org",
                                         "password": "password123", "role": "officer"})
    rows = admin.get("/api/admin/activity").json()
    assert rows and rows[0]["action"] == "user_created" and "password" not in str(rows).lower()
    assert make_user("officer").get("/api/admin/activity").status_code == 403
    assert make_user("citizen").get("/api/admin/activity").status_code == 403


def test_pages_revalidate_and_api_is_not_cached(client):
    assert client.get("/js/api.js").headers["cache-control"] == "no-cache"
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/api/health").headers["cache-control"] == "no-store"


def test_no_route_operates_utilities():
    """Coordination is recorded, never executed: no write endpoint operates field equipment."""
    verbs = re.compile(r"/(operate|valve|breaker|shutoff|shut-off|isolate|switch)(/|$)", re.I)
    for r in main.app.routes:
        methods = getattr(r, "methods", set()) or set()
        if methods & {"POST", "PUT", "PATCH", "DELETE"}:
            assert not verbs.search(getattr(r, "path", "")), r.path
