import base64
import json
import time

import pytest

import auth
from conftest import file_complaint


def test_register_login_and_me(client):
    r = client.post("/api/auth/register", json={"name": "Asha", "email": "Asha@Example.org", "password": "password123"})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "citizen"
    r = client.post("/api/auth/login", json={"email": "asha@example.org", "password": "password123"})
    assert r.status_code == 200
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + r.json()["token"]}).json()
    assert me["email"] == "asha@example.org" and "password_hash" not in me


def test_register_ignores_role_escalation(client):
    r = client.post("/api/auth/register", json={"name": "Mallory", "email": "m@example.org",
                                                "password": "password123", "role": "admin"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "citizen"
    h = {"Authorization": "Bearer " + r.json()["token"]}
    assert client.get("/api/admin/users", headers=h).status_code == 403


@pytest.mark.parametrize("body,code", [
    ({"name": "X Y", "email": "bad-email", "password": "password123"}, 422),
    ({"name": "X Y", "email": "a@example.org", "password": "short"}, 422),
])
def test_register_validation(client, body, code):
    assert client.post("/api/auth/register", json=body).status_code == code


def test_duplicate_registration(client, make_user):
    make_user("citizen")
    r = client.post("/api/auth/register", json={"name": "Again", "email": "citizen1@example.org", "password": "password123"})
    assert r.status_code == 409


def test_invalid_credentials_and_throttle(client, make_user):
    make_user("citizen")
    for _ in range(5):
        r = client.post("/api/auth/login", json={"email": "citizen1@example.org", "password": "wrong-password"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid email or password"
    r = client.post("/api/auth/login", json={"email": "citizen1@example.org", "password": "password123"})
    assert r.status_code == 429  # locked out even with the right password


def test_unknown_email_same_message(client):
    r = client.post("/api/auth/login", json={"email": "nobody@example.org", "password": "whatever123"})
    assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password"


def test_missing_invalid_and_expired_tokens(client, make_user):
    u = make_user("citizen")
    assert client.get("/api/complaints").status_code == 401
    assert client.get("/api/complaints", headers={"Authorization": "Bearer garbage"}).status_code == 401
    expired = auth.create_token(u.user["id"], "citizen", ttl_hours=-1)
    assert client.get("/api/complaints", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    # tampered payload (role changed) must fail the signature check
    body, sig = u.token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["role"] = "admin"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + "." + sig
    assert client.get("/api/admin/users", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_role_in_token_is_not_trusted(client, make_user):
    """A validly signed token claiming 'admin' for a citizen still gets citizen rights,
    because the role is re-read from the database on every request."""
    u = make_user("citizen")
    token = auth.create_token(u.user["id"], "admin")
    assert client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_logout_all_revokes_tokens(client, make_user):
    u = make_user("citizen")
    assert u.post("/api/auth/logout-all").status_code == 200
    assert u.get("/api/complaints").status_code == 401


def test_password_change(client, make_user):
    u = make_user("citizen")
    assert u.post("/api/auth/password", json={"current_password": "nope-nope", "new_password": "newpassword1"}).status_code == 403
    r = u.post("/api/auth/password", json={"current_password": "password123", "new_password": "newpassword1"})
    assert r.status_code == 200
    assert u.get("/api/complaints").status_code == 401  # old token revoked
    assert client.post("/api/auth/login", json={"email": u.user["email"], "password": "newpassword1"}).status_code == 200


# ---------------------------------------------------------------------------
# Route authorisation matrix
# ---------------------------------------------------------------------------
STAFF_ONLY = [
    ("GET", "/api/incidents"), ("POST", "/api/incidents/detect"), ("GET", "/api/dashboard"),
    ("GET", "/api/dashboard/charts"), ("POST", "/api/incidents/1/investigate"), ("POST", "/api/incidents/1/plans"),
    ("POST", "/api/actions/1/approve"), ("POST", "/api/actions/1/reject"), ("POST", "/api/actions/1/start"),
    ("POST", "/api/actions/1/complete"), ("POST", "/api/incidents/1/verify"), ("GET", "/api/complaints/1/plan"),
    ("POST", "/api/complaints/1/resolve"), ("GET", "/api/assets"), ("GET", "/api/map/assets"),
    ("GET", "/api/risk/findings"), ("POST", "/api/risk/scan"), ("POST", "/api/assets/1/maintenance"),
]
ADMIN_ONLY = [
    ("GET", "/api/admin/users"), ("POST", "/api/admin/users"), ("GET", "/api/admin/config"),
    ("GET", "/api/admin/stats"), ("GET", "/api/admin/complaints"), ("GET", "/api/admin/resolutions"),
    ("POST", "/api/admin/reset"), ("POST", "/api/assets"), ("POST", "/api/assets/import"),
]


def _call(actor, method, path):
    return actor.get(path) if method == "GET" else actor.post(path, json={})


def test_unauthenticated_is_rejected_everywhere(client):
    for method, path in STAFF_ONLY + ADMIN_ONLY + [("GET", "/api/complaints"), ("GET", "/api/notifications")]:
        r = client.get(path) if method == "GET" else client.post(path, json={})
        assert r.status_code == 401, (method, path, r.status_code)


def test_citizen_blocked_from_staff_and_admin_routes(client, make_user):
    c = make_user("citizen")
    for method, path in STAFF_ONLY + ADMIN_ONLY:
        assert _call(c, method, path).status_code == 403, (method, path)


def test_officer_blocked_from_admin_routes(client, make_user):
    o = make_user("officer")
    for method, path in ADMIN_ONLY:
        assert _call(o, method, path).status_code == 403, (method, path)


def test_only_citizens_file_complaints(client, make_user):
    assert file_complaint(make_user("officer")).status_code == 403
    assert file_complaint(make_user("admin")).status_code == 403
    assert file_complaint(make_user("citizen")).status_code == 200


def test_admin_user_listing_has_no_secrets(client, make_user):
    a = make_user("admin")
    rows = a.get("/api/admin/users").json()
    assert rows and all("password_hash" not in r and "token_version" not in r for r in rows)
    r = a.post("/api/admin/users", json={"name": "New Officer", "email": "o@example.org",
                                         "password": "password123", "role": "superuser"})
    assert r.status_code == 422


def test_no_endpoint_or_page_lists_accounts(client):
    """Account e-mails and passwords are never served — not even in demo mode."""
    for path in ("/api/demo-credentials", "/api/demo-info"):
        assert client.get(path).status_code == 404
    page = client.get("/index.html").text + client.get("/js/app.js").text
    assert "@civicmind.in" not in page and "demo-info" not in page and "demoBox" not in page


def test_production_mode_refuses_unsafe_configuration(client, monkeypatch, tmp_path):
    import config
    import main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setenv("CIVICMIND_SECRET", "short")
    assert any("CIVICMIND_SECRET" in p for p in config.production_problems())
    with pytest.raises(RuntimeError, match="will not start in production"):
        with TestClient(main.app):
            pass
    monkeypatch.setenv("CIVICMIND_SECRET", "x" * 48)
    monkeypatch.setattr(config, "SECRET_FROM_ENV", True)
    monkeypatch.setattr(config, "DEMO_MODE", True)
    assert config.production_problems() == ["CIVICMIND_DEMO_MODE must not be enabled in production."]
    monkeypatch.setattr(config, "DEMO_MODE", False)
    assert config.production_problems() == []
    monkeypatch.setattr(config, "IS_PRODUCTION", False)
    monkeypatch.setenv("CIVICMIND_SECRET", "")
    assert config.production_problems() == []      # development stays permissive


def test_hsts_only_over_https(client):
    assert "strict-transport-security" not in client.get("/api/health").headers
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app, base_url="https://civicmind.example") as secure:
        assert secure.get("/api/health").headers["strict-transport-security"].startswith("max-age=")


def test_reset_refused_on_non_demo_database(client, make_user):
    a = make_user("admin")
    assert a.post("/api/admin/reset", json={"confirm": "RESET"}).status_code == 403
    assert a.post("/api/admin/reset", json={"confirm": "nope"}).status_code == 422


def test_errors_do_not_leak_internals(client, make_user):
    a = make_user("admin")
    r = a.post("/api/complaints/abc/resolve", json={})
    assert r.status_code in (403, 422)
    assert "Traceback" not in r.text and ".py" not in r.text and "sqlite" not in r.text.lower()


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_oversized_request_rejected(client, make_user):
    c = make_user("citizen")
    huge = "data:image/jpeg;base64," + "A" * (10 * 1024 * 1024)
    r = c.post("/api/complaints", json={"category": "Pothole", "description": "x" * 20,
                                        "latitude": 18.52, "longitude": 73.85, "image": huge})
    assert r.status_code == 413


def test_token_has_expiry_claims(client, make_user):
    u = make_user("citizen")
    claims = auth.decode_token(u.token)
    assert claims["exp"] > time.time() and claims["typ"] == "access" and "iat" in claims
