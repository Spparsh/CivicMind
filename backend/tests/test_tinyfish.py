"""TinyFish adapter, URL safety layer and admin data-source routes.

The provider is always mocked: a fake transport stands in for `requests`, so
no test talks to TinyFish and no real key is needed."""
import logging

import pytest
import requests

import config
from database import query
from services.tinyfish import client as tf
from services.tinyfish import url_safety
from services.tinyfish.client import TinyFishClient, TinyFishError
from services.tinyfish.url_safety import UnsafeURL, normalize_public_url

KEY = "tf-test-SECRET-key-123"


class FakeResp:
    def __init__(self, status=200, data=None, raise_json=False, headers=None):
        self.status_code, self._data, self._raise, self.headers = status, data, raise_json, headers or {}

    def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._data


class FakeSession:
    """Returns queued responses in order (the last one repeats); records every call."""

    def __init__(self, *responses):
        self.queue, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        r = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(r, Exception):
            raise r
        if callable(r):
            return r(method, url, kw)
        return r


def make(session, **kw):
    kw.setdefault("cache_ttl", 0)
    return TinyFishClient(api_key=KEY, session=session, mock=False, sleep=lambda s: None, resolve_dns=False, **kw)


SEARCH_OK = {"query": "pmc water", "page": 0, "total_results": 4, "results": [
    {"position": 1, "site_name": "pmc.gov.in", "title": "Water supply GIS", "snippet": "Ward-wise water mains",
     "url": "https://pmc.gov.in/en/water-gis", "date": "2026-03-01", "unexpected": "dropped"},
    {"position": 2, "site_name": "x", "title": "Local file", "snippet": "", "url": "file:///etc/passwd"},
    {"position": 3, "site_name": "x", "title": "Loopback", "snippet": "", "url": "http://127.0.0.1/admin"},
    {"position": 4, "site_name": "opencity.in", "title": "Pune open data", "snippet": "Datasets",
     "url": "https://data.opencity.in/organization/pune"},
]}


def fetch_payload(results=(), errors=()):
    return {"results": list(results), "errors": list(errors)}


def page(url, text="# Water network\nMain lines by ward.", final_url=None):
    return {"url": url, "final_url": final_url or url, "title": "Water network", "description": None,
            "language": "en", "author": None, "published_date": None, "text": text, "latency_ms": 300,
            "format": "markdown"}


@pytest.fixture(autouse=True)
def _clean_cache():
    tf.CACHE.clear()
    yield
    tf.CACHE.clear()


# =========================================================================== client
def test_search_success_normalizes_and_uses_documented_request():
    s = FakeSession(FakeResp(200, SEARCH_OK))
    out = make(s).search_sources("pmc water", include_domains=["pmc.gov.in"], location="IN")
    call = s.calls[0]
    assert call["method"] == "GET" and call["url"] == "https://api.search.tinyfish.ai"
    assert call["params"]["query"] == "pmc water" and call["params"]["include_domains"] == "pmc.gov.in"
    assert call["headers"]["X-API-Key"] == KEY
    assert call["allow_redirects"] is False and call["timeout"]
    assert out["provider_mode"] == "live" and out["trust"] == "untrusted_candidate"
    assert [c["url"] for c in out["candidates"]] == ["https://pmc.gov.in/en/water-gis",
                                                     "https://data.opencity.in/organization/pune"]
    assert out["dropped"] == 2  # file:// and loopback results never become candidates
    first = out["candidates"][0]
    assert first["result_date"] == "2026-03-01" and "unexpected" not in first
    assert first["provenance"]["provider"] == "tinyfish_search" and first["provenance"]["query"] == "pmc water"


def test_missing_key_reports_not_configured_without_calling():
    s = FakeSession(FakeResp(200, SEARCH_OK))
    c = TinyFishClient(api_key="", session=s, mock=False)
    for call in (lambda: c.search_sources("pmc water"), lambda: c.fetch_sources(["https://pmc.gov.in/"]),
                 lambda: c.run_public_agent("https://pmc.gov.in/", "Read the contact table on this page")):
        with pytest.raises(TinyFishError) as e:
            call()
        assert e.value.code == "not_configured" and e.value.http_status == 503
    assert s.calls == []


def test_invalid_key_is_typed_and_not_retried():
    s = FakeSession(FakeResp(401, {"error": {"code": "INVALID_API_KEY", "message": "The provided API key is invalid"}}))
    with pytest.raises(TinyFishError) as e:
        make(s).search_sources("pmc water")
    assert e.value.code == "invalid_key" and KEY not in e.value.message
    assert len(s.calls) == 1


def test_timeout_retries_search_but_not_fetch():
    s = FakeSession(requests.Timeout("slow"))
    with pytest.raises(TinyFishError) as e:
        make(s).search_sources("pmc water")
    assert e.value.code == "timeout" and e.value.http_status == 504 and len(s.calls) == 3
    s2 = FakeSession(requests.Timeout("slow"))
    with pytest.raises(TinyFishError) as e2:
        make(s2).fetch_sources(["https://pmc.gov.in/a"])
    assert e2.value.code == "timeout" and len(s2.calls) == 1  # a timed-out batch may have been billed


@pytest.mark.parametrize("status,code", [(500, "provider_unavailable"), (503, "provider_unavailable"),
                                         (402, "insufficient_credits"), (403, "forbidden"),
                                         (400, "invalid_request"), (429, "rate_limited")])
def test_http_errors_are_mapped(status, code):
    s = FakeSession(FakeResp(status, {"error": {"code": "X", "message": "raw provider text " + KEY}}))
    with pytest.raises(TinyFishError) as e:
        make(s).search_sources("pmc water")
    assert e.value.code == code
    assert "raw provider text" not in e.value.message and KEY not in e.value.message


def test_fetch_retries_only_when_batch_was_not_processed():
    s = FakeSession(FakeResp(500, None))
    with pytest.raises(TinyFishError):
        make(s).fetch_sources(["https://pmc.gov.in/a"])
    assert len(s.calls) == 1
    s2 = FakeSession(FakeResp(503, None), FakeResp(200, fetch_payload([page("https://pmc.gov.in/a")])))
    out = make(s2).fetch_sources(["https://pmc.gov.in/a"])
    assert len(s2.calls) == 2 and len(out["results"]) == 1


def test_free_allowance_exhausted_is_distinct():
    s = FakeSession(FakeResp(402, {"error": {"code": "FREE_ALLOWANCE_EXHAUSTED", "message": "x"}}))
    with pytest.raises(TinyFishError) as e:
        make(s).search_sources("pmc water")
    assert e.value.code == "allowance_exhausted"


@pytest.mark.parametrize("resp", [FakeResp(200, raise_json=True), FakeResp(200, {"results": "nope"}),
                                  FakeResp(200, ["not", "an", "object"])])
def test_malformed_responses(resp):
    with pytest.raises(TinyFishError) as e:
        make(FakeSession(resp)).search_sources("pmc water")
    assert e.value.code == "bad_response"


def test_redirects_from_provider_are_not_followed():
    s = FakeSession(FakeResp(302, None, headers={"Location": "https://elsewhere.example.com"}))
    with pytest.raises(TinyFishError) as e:
        make(s).search_sources("pmc water")
    assert e.value.code == "provider_failed" and s.calls[0]["allow_redirects"] is False


def test_key_is_only_sent_to_https_tinyfish_hosts():
    s = FakeSession(FakeResp(200, SEARCH_OK))
    for bad in ("http://api.search.tinyfish.ai", "https://tinyfish.ai.evil.example.com", "https://example.com"):
        with pytest.raises(TinyFishError) as e:
            make(s, search_url=bad).search_sources("pmc water")
        assert e.value.code == "misconfigured"
    assert s.calls == []


def test_fetch_mixed_success_failure_and_redirect_checks():
    a, b, c = "https://pmc.gov.in/a", "https://pmc.gov.in/b", "https://pmc.gov.in/c"
    payload = fetch_payload(
        results=[page(a),
                 page(b, final_url="http://169.254.169.254/latest/meta-data"),       # unsafe redirect
                 page("https://not-requested.example.org/x")],                        # never selected
        errors=[{"url": c, "error": "page_not_found", "status": 404}])
    s = FakeSession(FakeResp(200, payload))
    out = make(s).fetch_sources([a, b, c])
    body = s.calls[0]["json"]
    assert s.calls[0]["method"] == "POST" and s.calls[0]["url"] == "https://api.fetch.tinyfish.ai"
    assert body["urls"] == [a, b, c] and body["format"] == "markdown"
    assert [r["url"] for r in out["results"]] == [a]
    assert len(out["results"][0]["content_sha256"]) == 64 and out["results"][0]["text"].startswith("# Water")
    errs = {e["url"]: e for e in out["errors"]}
    assert errs[b]["error"] == "unsafe_redirect" and "169.254" not in out["results"][0]["text"]
    assert errs[c]["error"] == "page_not_found" and errs[c]["status"] == 404
    assert "https://not-requested.example.org/x" not in errs


def test_fetch_flags_domain_change_and_truncates():
    a = "https://pmc.gov.in/a"
    s = FakeSession(FakeResp(200, fetch_payload([page(a, text="x" * 5000, final_url="https://other-site.org/a")])))
    out = make(s, max_content_chars=1000).fetch_sources([a])
    r = out["results"][0]
    assert "redirected_to_different_domain" in r["warnings"] and "content_truncated" in r["warnings"]
    assert len(r["text"]) == 1000 and r["text_truncated"] is True


def test_fetch_cap_is_enforced_before_any_call():
    s = FakeSession(FakeResp(200, fetch_payload()))
    with pytest.raises(TinyFishError) as e:
        make(s).fetch_sources([f"https://pmc.gov.in/p{i}" for i in range(11)])
    assert e.value.code == "too_many_urls" and s.calls == []


def test_unsafe_urls_are_never_sent():
    s = FakeSession(FakeResp(200, fetch_payload([page("https://pmc.gov.in/ok")])))
    out = make(s).fetch_sources(["http://localhost/a", "file:///etc/passwd", "https://pmc.gov.in/ok"])
    assert s.calls[0]["json"]["urls"] == ["https://pmc.gov.in/ok"]
    assert sorted(e["error"] for e in out["errors"]) == ["internal_host", "unsupported_scheme"]
    assert all(e["stage"] == "safety_check" for e in out["errors"])
    s2 = FakeSession(FakeResp(200, fetch_payload()))
    out2 = make(s2).fetch_sources(["http://10.0.0.1/"])
    assert s2.calls == [] and out2["errors"][0]["error"] == "private_address"


def test_fetch_reports_urls_missing_from_response():
    s = FakeSession(FakeResp(200, fetch_payload()))
    out = make(s).fetch_sources(["https://pmc.gov.in/a"])
    assert out["errors"][0]["error"] == "missing_from_response"


def test_search_cache_avoids_repeat_calls():
    s = FakeSession(FakeResp(200, SEARCH_OK))
    c = make(s, cache_ttl=60)
    first, second = c.search_sources("pmc water"), c.search_sources("pmc water")
    assert len(s.calls) == 1 and first["cached"] is False and second["cached"] is True


def test_agent_is_bounded_and_never_retried():
    s = FakeSession(FakeResp(200, {"run_id": "r1", "status": "COMPLETED", "started_at": None, "finished_at": None,
                                   "num_of_steps": 3, "result": {"items": []}, "error": None}))
    out = make(s).run_public_agent("https://pmc.gov.in/contacts", "Read the public contact table on this page",
                                   {"type": "object"})
    call = s.calls[0]
    assert call["url"] == "https://agent.tinyfish.ai/v1/automation/run"
    assert call["json"]["agent_config"]["max_duration_seconds"] == config.TINYFISH_AGENT_MAX_DURATION_SECONDS
    assert "max_steps" not in call["json"]["agent_config"]  # beta-gated; only sent when configured
    assert out["status"] == "COMPLETED" and out["trust"] == "untrusted_candidate"
    s2 = FakeSession(FakeResp(500, {"error": {"code": "INTERNAL_ERROR", "message": "x"}}))
    with pytest.raises(TinyFishError):
        make(s2).run_public_agent("https://pmc.gov.in/contacts", "Read the public contact table on this page")
    assert len(s2.calls) == 1


def test_mock_mode_is_labelled_and_needs_no_key():
    c = TinyFishClient(api_key="", mock=True, sleep=lambda s: None)
    out = c.search_sources("pmc water")
    assert out["provider_mode"] == "mock"
    assert all(x["title"].startswith("[MOCK]") and "example.org" in x["url"] for x in out["candidates"])
    f = c.fetch_sources([out["candidates"][0]["url"]])
    assert f["provider_mode"] == "mock" and f["results"][0]["title"].startswith("[MOCK]")


def test_key_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    make(FakeSession(FakeResp(200, SEARCH_OK))).search_sources("pmc water")
    with pytest.raises(TinyFishError):
        make(FakeSession(FakeResp(401, {"error": {"code": "INVALID_API_KEY", "message": KEY}}))).search_sources("x y")
    assert caplog.records and KEY not in caplog.text


# =========================================================================== URL safety
@pytest.mark.parametrize("url,code", [
    ("file:///etc/passwd", "unsupported_scheme"), ("ftp://pmc.gov.in/x", "unsupported_scheme"),
    ("javascript:alert(1)", "unsupported_scheme"), ("gopher://pmc.gov.in", "unsupported_scheme"),
    ("http://localhost/", "internal_host"), ("http://LOCALHOST./", "internal_host"),
    ("http://intranet/", "internal_host"), ("http://printer.local/", "internal_host"),
    ("http://metadata.google.internal/", "internal_host"), ("http://127.0.0.1/", "private_address"),
    ("http://10.1.2.3/", "private_address"), ("http://192.168.0.1:8080/", "private_address"),
    ("http://172.16.5.5/", "private_address"), ("http://169.254.169.254/latest/", "private_address"),
    ("http://100.64.0.1/", "private_address"), ("http://0.0.0.0/", "private_address"),
    ("http://[::1]/", "private_address"), ("http://[::ffff:127.0.0.1]/", "private_address"),
    ("http://[fe80::1%25eth0]/", "private_address"), ("http://2130706433/", "numeric_host"),
    ("http://0x7f.1/", "numeric_host"), ("http://127.1/", "numeric_host"),
    ("https://user:pass@pmc.gov.in/", "credentials_in_url"), ("https://pmc.gov.in/a b", "invalid_url"),
    ("", "invalid_url"), ("https://" + "a" * 2100 + ".com", "invalid_url"),
])
def test_unsafe_urls_rejected(url, code):
    with pytest.raises(UnsafeURL) as e:
        normalize_public_url(url)
    assert e.value.code == code


@pytest.mark.parametrize("url,expected", [
    ("https://PMC.gov.in/en/Water?x=1#frag", "https://pmc.gov.in/en/Water?x=1"),
    ("http://data.opencity.in", "http://data.opencity.in/"),
    ("https://pmc.gov.in:443/a", "https://pmc.gov.in/a"),
    ("https://gis.example.org:6443/arcgis/rest", "https://gis.example.org:6443/arcgis/rest"),
    ("https://8.8.8.8/x", "https://8.8.8.8/x"),
])
def test_public_urls_normalized(url, expected):
    assert normalize_public_url(url) == expected


def test_dns_resolution_blocks_private_targets(monkeypatch):
    monkeypatch.setattr(url_safety, "resolver", lambda host: {"10.0.0.5"})
    with pytest.raises(UnsafeURL) as e:
        normalize_public_url("https://sneaky.example.com/", resolve=True)
    assert e.value.code == "private_address"
    monkeypatch.setattr(url_safety, "resolver", lambda host: {"93.184.216.34", "::ffff:127.0.0.1"})
    with pytest.raises(UnsafeURL):
        normalize_public_url("https://mixed.example.com/", resolve=True)

    def boom(host):
        raise OSError("nxdomain")
    monkeypatch.setattr(url_safety, "resolver", boom)
    with pytest.raises(UnsafeURL) as e3:
        normalize_public_url("https://nowhere.example.com/", resolve=True)
    assert e3.value.code == "unresolvable_host"
    monkeypatch.setattr(url_safety, "resolver", lambda host: {"93.184.216.34"})
    assert normalize_public_url("https://ok.example.com/", resolve=True) == "https://ok.example.com/"


# =========================================================================== routes
@pytest.fixture()
def provider(monkeypatch):
    """Install a fake TinyFish transport behind the admin routes."""
    holder = {"session": FakeSession(FakeResp(200, SEARCH_OK))}
    monkeypatch.setattr(config, "TINYFISH_API_KEY", KEY)
    monkeypatch.setattr(config, "TINYFISH_MOCK_MODE", False)
    monkeypatch.setattr(tf, "get_client", lambda **kw: make(holder["session"]))
    return holder


def _table_counts():
    tables = [r["name"] for r in query("SELECT name FROM sqlite_master WHERE type='table'")]
    return {t: query(f"SELECT COUNT(*) c FROM {t}", one=True)["c"] for t in tables
            if t in ("assets", "asset_maintenance", "complaint_assets") or t.startswith("utility_")}


DISCOVER = {"authority": "Pune Municipal Corporation", "region": "Pune", "topic": "water supply network",
            "data_types": ["gis_layer", "open_data"], "include_domains": ["pmc.gov.in"]}


def test_data_source_routes_are_admin_only(client, make_user, provider):
    citizen, officer = make_user("citizen"), make_user("officer")
    paths = [("get", "/api/admin/data-sources/status", None), ("post", "/api/admin/data-sources/discover", DISCOVER),
             ("post", "/api/admin/data-sources/fetch", {"urls": ["https://pmc.gov.in/a"]}),
             ("get", "/api/admin/data-sources/snapshots", None),
             ("post", "/api/admin/data-sources/agent-extract", {"url": "https://pmc.gov.in/", "extract": "x" * 12})]
    for who in (citizen, officer):
        for method, path, body in paths:
            r = who.get(path) if method == "get" else who.post(path, json=body)
            assert r.status_code == 403, (path, r.status_code)
    assert client.get("/api/admin/data-sources/status").status_code == 401
    assert provider["session"].calls == []


def test_not_configured_state_is_clear(client, make_user, monkeypatch):
    monkeypatch.setattr(config, "TINYFISH_API_KEY", "")
    monkeypatch.setattr(config, "TINYFISH_MOCK_MODE", False)
    admin = make_user("admin")
    st = admin.get("/api/admin/data-sources/status").json()
    assert st["configured"] is False and st["provider_mode"] == "not_configured" and "TINYFISH_API_KEY" in st["message"]
    assert st["agent_enabled"] is False and st["utility_import_available"] is True
    r = admin.post("/api/admin/data-sources/discover", json=DISCOVER)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "not_configured"
    r = admin.post("/api/admin/data-sources/fetch", json={"urls": ["https://pmc.gov.in/a"]})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "not_configured"
    assert client.get("/api/health").status_code == 200  # the rest of the app keeps working


def test_discover_fetch_review_flow_never_writes_assets(client, make_user, provider):
    admin = make_user("admin")
    before = _table_counts()
    r = admin.post("/api/admin/data-sources/discover", json=DISCOVER)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["provider_mode"] == "live" and d["trust"] == "untrusted_candidate" and d["dropped"] == 2
    sent = provider["session"].calls[0]["params"]
    assert "Pune Municipal Corporation" in sent["query"] and sent["include_domains"] == "pmc.gov.in"
    cands = d["candidates"]
    assert len(cands) == 2 and all(c["review_status"] == "candidate" for c in cands)
    assert cands[0]["provenance"]["provider"] == "tinyfish_search"

    first = cands[0]
    provider["session"] = FakeSession(FakeResp(200, fetch_payload([page(first["url"])])))
    r = admin.post("/api/admin/data-sources/fetch", json={"candidate_ids": [first["id"]]})
    assert r.status_code == 200, r.text
    snap = r.json()["snapshots"][0]
    assert snap["review_status"] == "pending_review" and snap["fetch_status"] == "ok"
    assert snap["candidate_id"] == first["id"] and snap["trust"] == "untrusted_candidate"
    listed = admin.get("/api/admin/data-sources/candidates", params={"run_id": d["run"]["id"]}).json()
    assert {c["id"]: c["review_status"] for c in listed["candidates"]}[first["id"]] == "selected"

    detail = admin.get(f"/api/admin/data-sources/snapshots/{snap['id']}").json()
    assert detail["content_text"].startswith("# Water network")
    assert detail["candidate"]["authority"] == "Pune Municipal Corporation"

    r = admin.get(f"/api/admin/data-sources/snapshots/{snap['id']}/source-metadata")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_reviewed"
    r = admin.post(f"/api/admin/data-sources/snapshots/{snap['id']}/review",
                   json={"decision": "reviewed", "note": "Official PMC page"})
    assert r.status_code == 200 and r.json()["review_status"] == "reviewed"
    meta = admin.get(f"/api/admin/data-sources/snapshots/{snap['id']}/source-metadata").json()
    assert meta["provenance_class"] == "official_unverified" and meta["authority"] == "Pune Municipal Corporation"
    assert meta["source_url"] == first["url"] and meta["review"]["status"] == "reviewed"
    assert meta["utility_import_available"] is True

    # Discovery and fetch never create or change infrastructure records.
    assert _table_counts() == before
    assert {"utility_assets", "utility_connections"} <= set(_table_counts())  # counted above: unchanged
    acts = [a["action"] for a in query("SELECT action FROM activity")]
    assert {"data_source_discovery", "data_source_fetch", "data_source_reviewed"} <= set(acts)


def test_fetch_limits_unsafe_urls_and_provider_errors(client, make_user, provider):
    admin = make_user("admin")
    r = admin.post("/api/admin/data-sources/fetch", json={"urls": [f"https://pmc.gov.in/{i}" for i in range(11)]})
    assert r.status_code == 422
    r = admin.post("/api/admin/data-sources/fetch", json={"urls": ["http://127.0.0.1/", "file:///etc/passwd"]})
    assert r.status_code == 200
    assert {e["error"] for e in r.json()["errors"]} == {"private_address", "unsupported_scheme"}
    assert provider["session"].calls == [] and query("SELECT COUNT(*) c FROM source_snapshots", one=True)["c"] == 0

    provider["session"] = FakeSession(FakeResp(200, fetch_payload(
        errors=[{"url": "https://pmc.gov.in/gone", "error": "page_not_found", "status": 404}])))
    r = admin.post("/api/admin/data-sources/fetch", json={"urls": ["https://pmc.gov.in/gone"]})
    err = r.json()["errors"][0]
    assert err["error"] == "page_not_found" and err["snapshot_id"]
    failed = admin.get(f"/api/admin/data-sources/snapshots/{err['snapshot_id']}").json()
    assert failed["fetch_status"] == "error" and failed["review_status"] == "rejected"
    r = admin.post(f"/api/admin/data-sources/snapshots/{err['snapshot_id']}/review", json={"decision": "reviewed"})
    assert r.status_code == 409

    provider["session"] = FakeSession(FakeResp(401, {"error": {"code": "INVALID_API_KEY", "message": KEY}}))
    r = admin.post("/api/admin/data-sources/fetch", json={"urls": ["https://pmc.gov.in/a"]})
    assert r.status_code == 502 and r.json()["detail"]["code"] == "invalid_key" and KEY not in r.text


def test_agent_disabled_by_default(client, make_user, provider):
    admin = make_user("admin")
    r = admin.post("/api/admin/data-sources/agent-extract",
                   json={"url": "https://pmc.gov.in/contacts", "extract": "Read the public contact table"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "agent_disabled"
    assert provider["session"].calls == []


def test_agent_domain_restriction_and_daily_cap(client, make_user, provider, monkeypatch):
    monkeypatch.setattr(config, "TINYFISH_ENABLE_AGENT", True)
    monkeypatch.setattr(config, "TINYFISH_AGENT_DAILY_LIMIT", 1)
    monkeypatch.setattr(config, "TINYFISH_AGENT_ALLOWED_DOMAINS", [])
    admin = make_user("admin")
    body = {"url": "https://pmc.gov.in/contacts", "extract": "Read the public water department contact table"}
    r = admin.post("/api/admin/data-sources/agent-extract", json=body)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "domain_not_allowed"

    cands = admin.post("/api/admin/data-sources/discover", json=DISCOVER).json()["candidates"]
    admin.post(f"/api/admin/data-sources/candidates/{cands[0]['id']}/review", json={"status": "selected"})
    provider["session"] = FakeSession(FakeResp(200, {"run_id": "run-1", "status": "COMPLETED", "started_at": None,
                                                     "finished_at": None, "num_of_steps": 4, "error": None,
                                                     "result": {"items": [{"label": "Control room", "value": "x",
                                                                           "source_url": body["url"]}]}}))
    r = admin.post("/api/admin/data-sources/agent-extract", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    goal = provider["session"].calls[0]["json"]["goal"]
    assert "do not log in" in goal and "pmc.gov.in" in goal
    assert out["snapshot"]["review_status"] == "pending_review" and out["snapshot"]["method"] == "tinyfish_agent"
    assert "agent_output_unverified" in out["snapshot"]["warnings"]
    assert out["usage"] == {"used_today": 1, "daily_limit": 1}
    r = admin.post("/api/admin/data-sources/agent-extract", json=body)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "agent_daily_limit"
    assert len(provider["session"].calls) == 1


def test_status_never_exposes_the_key(client, make_user, provider):
    admin = make_user("admin")
    r = admin.get("/api/admin/data-sources/status")
    assert r.status_code == 200 and KEY not in r.text
    st = r.json()
    assert st["configured"] is True and st["api_key_present"] is True and st["max_fetch_urls"] == 10
    assert st["endpoints"]["fetch"] == "api.fetch.tinyfish.ai"
