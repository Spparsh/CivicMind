from conftest import file_complaint, image_data_url, offset, PUNE


def _cluster(make_user, n=3, category="Streetlight Failure"):
    citizens = [make_user("citizen") for _ in range(n)]
    res = []
    for i, c in enumerate(citizens):
        lat, lng = offset(*PUNE, dlat_m=20 * i)
        r = file_complaint(c, category, lat, lng, text=f"Streetlight number {i} is not working at night, dark street.")
        assert r.status_code == 200, r.text
        res.append(r.json())
    return citizens, res


def test_citizen_sees_only_own_complaints(client, make_user):
    a, b = make_user("citizen"), make_user("citizen")
    ca = file_complaint(a).json()
    cb = file_complaint(b, text="Different pothole description near the market.").json()
    mine = a.get("/api/complaints").json()
    assert [c["id"] for c in mine] == [ca["id"]]
    assert a.get(f"/api/complaints/{cb['id']}").status_code == 404
    assert a.get(f"/api/complaints/{cb['id']}/photo").status_code == 404
    # the old `mine=0` escape hatch is gone
    assert [c["id"] for c in a.get("/api/complaints?mine=0").json()] == [ca["id"]]


def test_staff_see_all_but_only_admin_sees_email(client, make_user):
    c = make_user("citizen")
    file_complaint(c)
    off = make_user("officer").get("/api/complaints").json()[0]
    adm = make_user("admin").get("/api/complaints").json()[0]
    assert "citizen_email" not in off and off["citizen_name"]
    assert adm["citizen_email"] == c.user["email"]
    assert "tracking_code" not in off


def test_public_tracking_is_minimal_and_not_enumerable(client, make_user):
    c = make_user("citizen")
    r = file_complaint(c, text="Secret detail: my house number is 42 and my phone is 98xxxx").json()
    t = client.get(f"/api/track/{r['tracking_code']}")
    assert t.status_code == 200
    body = t.json()
    blob = str(body)
    for forbidden in ("Secret detail", "latitude", "longitude", "description", c.user["email"],
                      c.user["name"], "image", "photo", "notes"):
        assert forbidden not in blob, forbidden
    assert set(body) == {"public_id", "category", "status", "ward", "submitted_on", "last_update",
                         "incident", "next_step"}
    assert client.get(f"/api/track/{r['public_id']}").status_code == 404  # sequential ids not accepted
    assert client.get("/api/track/AAAAAAAAAA").status_code == 404


def test_incident_detail_respects_ownership(client, make_user):
    citizens, res = _cluster(make_user)
    inc_id = res[-1]["incident_id"]
    assert inc_id, "three nearby related reports should form an incident"
    outsider = make_user("citizen")
    assert outsider.get(f"/api/incidents/{inc_id}").status_code == 404
    view = citizens[0].get(f"/api/incidents/{inc_id}").json()
    assert view["view"] == "citizen"
    blob = str(view)
    for other in citizens[1:]:
        assert other.user["name"] not in blob
    assert "complaints" not in view and "description" not in blob
    assert [r["id"] for r in view["my_reports"]] == [res[0]["id"]]
    staff = make_user("officer").get(f"/api/incidents/{inc_id}").json()
    assert staff["view"] == "staff" and len(staff["complaints"]) == 3


def test_map_endpoints_apply_same_policy(client, make_user):
    citizens, res = _cluster(make_user)
    outsider = make_user("citizen")
    assert outsider.get("/api/map/complaints").json() == []
    incs = outsider.get("/api/map/incidents").json()
    assert incs and all("members" not in i and i["id"] is None and not i["linked"] for i in incs)
    mine = citizens[0].get("/api/map/incidents").json()
    assert mine[0]["linked"] and mine[0]["id"] == res[-1]["incident_id"]
    assert len(make_user("officer").get("/api/map/complaints").json()) == 3


def test_evidence_not_publicly_served(client, make_user):
    c = make_user("citizen")
    r = file_complaint(c, image=image_data_url(5)).json()
    assert r["photo"]["verdict"] == "passed"
    assert client.get(f"/api/complaints/{r['id']}/photo").status_code == 401
    assert c.get(f"/api/complaints/{r['id']}/photo").status_code == 200
    assert make_user("citizen").get(f"/api/complaints/{r['id']}/photo").status_code == 404
    assert make_user("officer").get(f"/api/complaints/{r['id']}/photo").status_code == 200
    assert client.get("/uploads/anything.jpg").status_code == 404
    detail = c.get(f"/api/complaints/{r['id']}").json()
    assert "image_path" not in detail and "private" not in str(detail)


def test_notifications_are_private(client, make_user):
    a, b = make_user("citizen"), make_user("citizen")
    file_complaint(a)
    assert a.get("/api/notifications").json()["items"]
    assert b.get("/api/notifications").json()["items"] == []


def test_citizen_dashboard_endpoints(client, make_user):
    c = make_user("citizen")
    file_complaint(c)
    assert c.get("/api/dashboard/citizen").json()["my_complaints"] == 1
    assert c.get("/api/dashboard").status_code == 403
