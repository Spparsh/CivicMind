"""
CivicMind — server-side TinyFish adapter (Search, Fetch, optional Agent).

Request/response formats follow docs.tinyfish.ai as verified on 2026-10-10
(see docs/CIVICMIND_INTEGRATION_CONTRACT.md, section B5):

  Search  GET  {TINYFISH_SEARCH_URL}?query=...        -> {query, results[], total_results, page}
  Fetch   POST {TINYFISH_FETCH_URL}  {"urls": [...]}  -> {results[], errors[]}   (max 10 URLs)
  Agent   POST {TINYFISH_AGENT_URL}/v1/automation/run -> {run_id, status, result, error, ...}

Rules this module enforces:

  * The API key travels only in the `X-API-Key` header of server-to-server
    requests, never in URLs, logs, error messages or return values. Redirects
    from TinyFish hosts are NOT followed (requests would forward custom
    headers such as X-API-Key to the redirect target).
  * Provider hosts must be https://*.tinyfish.ai.
  * Bounded timeouts. Retries only where repeating is safe: Search (a GET)
    on 429/5xx/network errors; Fetch only when the service said it did not
    process the batch (429/503) or the connection was never made; Agent runs
    are metered and never retried.
  * Responses are normalized defensively — unknown fields are dropped, text
    is length-capped, every result is marked as untrusted candidate content.
  * Mock mode (TINYFISH_MOCK_MODE=true, tests/local only) returns fixtures
    that are labelled `provider_mode: "mock"` and "[MOCK]" in every title.
    Live mode never fabricates output.
"""
import hashlib
import json
import logging
import threading
import time
from urllib.parse import urlsplit

import requests

import config
from database import now
from services.tinyfish import url_safety
from services.tinyfish.url_safety import UnsafeURL

log = logging.getLogger("civicmind.tinyfish")

SEARCH_PROVIDER = "tinyfish_search"
FETCH_PROVIDER = "tinyfish_fetch"
AGENT_PROVIDER = "tinyfish_agent"
FETCH_FORMATS = ("markdown", "html", "json")
FETCH_ERROR_CODES = {
    "target_http_error", "page_not_found", "target_unreachable", "timeout", "bot_blocked", "empty_content",
    "login_required", "content_too_large", "invalid_url", "invalid_redirect_url", "proxy_error",
    "conditional_unsupported", "selector_not_matched", "selector_unsupported",
}
UNTRUSTED_NOTE = ("Untrusted public web content. It does not establish asset geometry, network "
                  "connectivity, flow direction, operator authority or contact validity.")


class TinyFishError(Exception):
    """Typed, secret-free provider error. `http_status` is what CivicMind returns."""

    def __init__(self, code, message, http_status=502, retryable=False, provider_status=None):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status
        self.retryable, self.provider_status = retryable, provider_status

    def to_dict(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable,
                "provider_status": self.provider_status}


_NOT_CONFIGURED = ("TinyFish is not configured. Set TINYFISH_API_KEY in the server environment "
                   "(.env) and restart CivicMind.")


def _s(val, limit):
    if val is None:
        return None
    if not isinstance(val, (str, int, float)):
        return None
    text = " ".join(str(val).split())
    return text[:limit] if text else None


def _provider_error(status, body):
    """Map a non-2xx TinyFish response to a TinyFishError (never echoes raw provider text)."""
    code = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "")[:60] or None
    if status == 401:
        return TinyFishError("invalid_key", "TinyFish rejected the configured API key. Check TINYFISH_API_KEY.",
                             502, False, status)
    if status == 402:
        if code == "FREE_ALLOWANCE_EXHAUSTED":
            return TinyFishError("allowance_exhausted", "The TinyFish daily free allowance is used up. "
                                 "It resets at 00:00 UTC.", 503, False, status)
        return TinyFishError("insufficient_credits", "TinyFish reports insufficient credits for this request.",
                             503, False, status)
    if status == 403:
        return TinyFishError("forbidden", "TinyFish refused this request for the configured account.",
                             502, False, status)
    if status == 429:
        if code == "DAILY_LIMIT_EXCEEDED":
            return TinyFishError("daily_limit", "The TinyFish daily limit for this capability is reached.",
                                 429, False, status)
        return TinyFishError("rate_limited", "TinyFish rate limit reached. Try again shortly.", 429, True, status)
    if status in (400, 422):
        return TinyFishError("invalid_request", "TinyFish rejected the request parameters.", 422, False, status)
    if status in (500, 502, 503, 504):
        return TinyFishError("provider_unavailable", "TinyFish is temporarily unavailable. Try again later.",
                             502, True, status)
    return TinyFishError("provider_failed", "TinyFish returned an unexpected response.", 502, False, status)


class _Cache:
    def __init__(self):
        self._data, self._lock = {}, threading.Lock()
        self.hits = 0

    def get(self, key, ttl):
        if ttl <= 0:
            return None
        with self._lock:
            item = self._data.get(key)
            if item and time.time() - item[0] < ttl:
                self.hits += 1
                return item[1]
            self._data.pop(key, None)
            return None

    def put(self, key, value):
        with self._lock:
            if len(self._data) > 500:
                self._data.clear()
            self._data[key] = (time.time(), value)

    def clear(self):
        with self._lock:
            self._data.clear()
            self.hits = 0


CACHE = _Cache()


class TinyFishClient:
    def __init__(self, api_key=None, *, search_url=None, fetch_url=None, agent_url=None, timeout=None,
                 fetch_timeout=None, session=None, mock=None, sleep=time.sleep, resolve_dns=None,
                 cache_ttl=None, max_fetch_urls=None, max_content_chars=None, max_search_results=None):
        self._key = (config.TINYFISH_API_KEY if api_key is None else api_key) or ""
        self.mock = config.TINYFISH_MOCK_MODE if mock is None else bool(mock)
        self.search_url = search_url or config.TINYFISH_SEARCH_URL
        self.fetch_url = fetch_url or config.TINYFISH_FETCH_URL
        self.agent_url = (agent_url or config.TINYFISH_AGENT_URL).rstrip("/")
        self.timeout = timeout or config.TINYFISH_TIMEOUT_SECONDS
        self.fetch_timeout = fetch_timeout or config.TINYFISH_FETCH_TIMEOUT_SECONDS
        self.resolve_dns = config.TINYFISH_RESOLVE_DNS if resolve_dns is None else resolve_dns
        self.cache_ttl = config.TINYFISH_CACHE_TTL_SECONDS if cache_ttl is None else cache_ttl
        self.max_fetch_urls = min(10, max_fetch_urls or config.TINYFISH_MAX_FETCH_URLS)
        self.max_content_chars = max_content_chars or config.TINYFISH_MAX_CONTENT_CHARS
        self.max_search_results = max_search_results or config.TINYFISH_MAX_SEARCH_RESULTS
        self._session = session
        self._sleep = sleep

    # ------------------------------------------------------------------ state
    @property
    def configured(self):
        return self.mock or bool(self._key)

    @property
    def mode(self):
        return "mock" if self.mock else "live"

    def _require_configured(self):
        if not self.configured:
            raise TinyFishError("not_configured", _NOT_CONFIGURED, 503, False)

    def _session_obj(self):
        if self._session is None:
            if self.mock:
                from services.tinyfish.mock import MockSession
                self._session = MockSession()
            else:
                self._session = requests.Session()
        return self._session

    @staticmethod
    def _check_endpoint(url):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not (host == "tinyfish.ai" or host.endswith(".tinyfish.ai")):
            raise TinyFishError("misconfigured", "TinyFish endpoint configuration is invalid "
                                "(must be https://*.tinyfish.ai).", 503, False)

    # ------------------------------------------------------------- transport
    def _call(self, method, url, *, params=None, body=None, timeout, retry_statuses=(), retry_network=False,
              retry_timeout=False, attempts=1):
        """One logical provider call with bounded, explicit retries. Returns the parsed JSON object."""
        self._require_configured()
        if not self.mock:
            self._check_endpoint(url)
        headers = {"X-API-Key": self._key, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        last = None
        for attempt in range(1, attempts + 1):
            try:
                resp = self._session_obj().request(method, url, params=params, json=body, headers=headers,
                                                   timeout=timeout, allow_redirects=False)
            except requests.Timeout:
                last = TinyFishError("timeout", "TinyFish did not respond in time.", 504, True)
                if retry_timeout and attempt < attempts:
                    self._sleep(min(4, attempt))
                    continue
                raise last from None
            except requests.RequestException:
                last = TinyFishError("provider_unavailable", "Could not reach TinyFish.", 502, True)
                if retry_network and attempt < attempts:
                    self._sleep(min(4, attempt))
                    continue
                raise last from None
            status = getattr(resp, "status_code", 0)
            # Host and status only — never headers, parameters or bodies.
            log.info("TinyFish %s %s -> HTTP %s (%s)", method, urlsplit(url).hostname, status, self.mode)
            try:
                data = resp.json()
            except ValueError:
                data = None
            if 300 <= status < 400:
                raise TinyFishError("provider_failed", "TinyFish answered with a redirect; it was not followed.",
                                    502, False, status)
            if status >= 400 or status < 200:
                last = _provider_error(status, data)
                if status in retry_statuses and attempt < attempts:
                    retry_after = 0
                    try:
                        retry_after = float((getattr(resp, "headers", None) or {}).get("Retry-After", 0))
                    except (TypeError, ValueError):
                        retry_after = 0
                    self._sleep(min(5, max(retry_after, attempt)))
                    continue
                raise last
            if not isinstance(data, dict):
                raise TinyFishError("bad_response", "TinyFish returned a response CivicMind could not read.",
                                    502, False, status)
            return data
        raise last  # pragma: no cover — loop always returns or raises

    # ---------------------------------------------------------------- search
    def search_sources(self, query, *, location=None, language=None, include_domains=None,
                       exclude_domains=None, page=0, purpose=None):
        """TinyFish Search -> normalized source candidates (untrusted)."""
        query = " ".join(str(query or "").split())
        if not 2 <= len(query) <= 400:
            raise TinyFishError("invalid_request", "The search query must be 2–400 characters.", 422)
        try:
            page = int(page or 0)
        except (TypeError, ValueError):
            page = 0
        if not 0 <= page <= 10:
            raise TinyFishError("invalid_request", "page must be between 0 and 10.", 422)
        params = {"query": query, "page": page}
        if location:
            params["location"] = str(location)[:8]
        if language:
            params["language"] = str(language)[:8]
        if include_domains:
            params["include_domains"] = ",".join(include_domains)[:1000]
        if exclude_domains:
            params["exclude_domains"] = ",".join(exclude_domains)[:1000]
        if purpose:
            params["purpose"] = str(purpose)[:2000]

        cache_key = "search:" + json.dumps(params, sort_keys=True)
        cached = None if self.mock else CACHE.get(cache_key, self.cache_ttl)
        if cached is not None:
            return dict(cached, cached=True)
        data = self._call("GET", self.search_url, params=params, timeout=self.timeout,
                          retry_statuses=(429, 500, 502, 503, 504), retry_network=True, retry_timeout=True,
                          attempts=3)
        raw = data.get("results")
        if not isinstance(raw, list):
            raise TinyFishError("bad_response", "TinyFish search response had no results list.", 502)
        retrieved_at = now()
        candidates, dropped = [], 0
        for item in raw[: self.max_search_results * 2]:
            cand = self._normalize_candidate(item, query, retrieved_at)
            if cand is None:
                dropped += 1
                continue
            candidates.append(cand)
            if len(candidates) >= self.max_search_results:
                break
        out = {"provider": SEARCH_PROVIDER, "provider_mode": self.mode, "query": query, "page": page,
               "candidates": candidates, "dropped": dropped, "retrieved_at": retrieved_at, "cached": False,
               "trust": "untrusted_candidate", "note": UNTRUSTED_NOTE}
        if not self.mock:
            CACHE.put(cache_key, out)
        return out

    def _normalize_candidate(self, item, query, retrieved_at):
        if not isinstance(item, dict):
            return None
        try:
            url = url_safety.normalize_public_url(item.get("url"), resolve=False)
        except UnsafeURL:
            return None
        try:
            position = int(item.get("position"))
        except (TypeError, ValueError):
            position = None
        title = _s(item.get("title"), 300) or url
        return {
            "url": url,
            "title": ("[MOCK] " + title) if self.mock and not title.startswith("[MOCK]") else title,
            "snippet": _s(item.get("snippet"), 1000),
            "site_name": _s(item.get("site_name"), 200) or url_safety.host_of(url),
            "result_date": _s(item.get("date"), 40),
            "position": position,
            "provenance": {"provider": SEARCH_PROVIDER, "provider_mode": self.mode, "query": query,
                           "retrieved_at": retrieved_at},
        }

    # ----------------------------------------------------------------- fetch
    def check_urls(self, urls):
        """Validate admin-selected URLs. Returns (accepted, rejected[{url, error, message}])."""
        if not isinstance(urls, list) or not urls:
            raise TinyFishError("invalid_request", "Select at least one URL to fetch.", 422)
        if len(urls) > self.max_fetch_urls:
            raise TinyFishError("too_many_urls", f"TinyFish Fetch accepts at most {self.max_fetch_urls} URLs "
                                "per request.", 422)
        accepted, rejected, seen = [], [], set()
        for raw in urls:
            try:
                url = url_safety.normalize_public_url(raw, resolve=self.resolve_dns and not self.mock)
            except UnsafeURL as e:
                rejected.append({"url": str(raw)[:300], "error": e.code, "message": e.message,
                                 "stage": "safety_check"})
                continue
            if url not in seen:
                seen.add(url)
                accepted.append(url)
        return accepted, rejected

    def fetch_sources(self, urls, *, fmt="markdown", links=False, ttl=None, per_url_timeout_ms=None,
                      purpose=None):
        """TinyFish Fetch of admin-selected public URLs -> per-URL results and errors (all untrusted)."""
        self._require_configured()
        if fmt not in FETCH_FORMATS:
            raise TinyFishError("invalid_request", "Unsupported fetch format.", 422)
        accepted, rejected = self.check_urls(urls)
        out = {"provider": FETCH_PROVIDER, "provider_mode": self.mode, "results": [], "errors": list(rejected),
               "retrieved_at": now(), "trust": "untrusted_candidate", "note": UNTRUSTED_NOTE, "cache_hits": 0}
        to_fetch = []
        for url in accepted:
            hit = None if self.mock else CACHE.get(f"fetch:{fmt}:{int(bool(links))}:{url}", self.cache_ttl)
            if hit is not None:
                out["results"].append(dict(hit, cached=True))
                out["cache_hits"] += 1
            else:
                to_fetch.append(url)
        if not to_fetch:
            return out
        body = {"urls": to_fetch, "format": fmt}
        if links:
            body["links"] = True
        if ttl is not None:
            body["ttl"] = max(0, int(ttl))
        if per_url_timeout_ms:
            body["per_url_timeout_ms"] = max(1, min(110_000, int(per_url_timeout_ms)))
        if purpose:
            body["purpose"] = str(purpose)[:2000]
        data = self._call("POST", self.fetch_url, body=body, timeout=self.fetch_timeout,
                          retry_statuses=(429, 503), retry_network=False, retry_timeout=False, attempts=2)
        raw_results, raw_errors = data.get("results"), data.get("errors", [])
        if not isinstance(raw_results, list) or not isinstance(raw_errors, list):
            raise TinyFishError("bad_response", "TinyFish fetch response was not in the documented shape.", 502)

        requested = set(to_fetch)
        answered = set()
        for item in raw_results:
            res = self._normalize_fetch_result(item, requested)
            if res is None:
                continue
            answered.add(res["url"])
            if res.get("error"):
                out["errors"].append(res)
                continue
            out["results"].append(res)
            if not self.mock:
                CACHE.put(f"fetch:{fmt}:{int(bool(links))}:{res['url']}", res)
        for item in raw_errors:
            if not isinstance(item, dict):
                continue
            url = self._match_requested(item.get("url"), requested)
            if not url:
                continue
            answered.add(url)
            code = str(item.get("error") or "")
            entry = {"url": url, "error": code if code in FETCH_ERROR_CODES else "unknown_error",
                     "message": "TinyFish could not fetch this URL."}
            try:
                if item.get("status") is not None:
                    entry["status"] = int(item["status"])
            except (TypeError, ValueError):
                pass
            out["errors"].append(entry)
        for url in to_fetch:
            if url not in answered:
                out["errors"].append({"url": url, "error": "missing_from_response",
                                      "message": "TinyFish did not return a result for this URL."})
        return out

    @staticmethod
    def _match_requested(raw, requested):
        if not isinstance(raw, str):
            return None
        if raw in requested:
            return raw
        try:
            norm = url_safety.normalize_public_url(raw, resolve=False)
        except UnsafeURL:
            return None
        return norm if norm in requested else None

    def _normalize_fetch_result(self, item, requested):
        if not isinstance(item, dict):
            return None
        url = self._match_requested(item.get("url"), requested)
        if not url:
            return None  # never accept content for a URL the admin did not select
        warnings = []
        final_raw = item.get("final_url") or url
        try:
            final_url = url_safety.normalize_public_url(final_raw, resolve=self.resolve_dns and not self.mock)
        except UnsafeURL as e:
            return {"url": url, "error": "unsafe_redirect",
                    "message": f"The page redirected to a disallowed address ({e.code}); content discarded."}
        if not url_safety.same_site(url_safety.host_of(url), url_safety.host_of(final_url)):
            warnings.append("redirected_to_different_domain")
        text = item.get("text")
        if isinstance(text, (dict, list)):
            text = json.dumps(text, ensure_ascii=False, indent=1)
        elif text is not None and not isinstance(text, str):
            text = str(text)
        text = text or ""
        truncated = len(text) > self.max_content_chars
        if truncated:
            text = text[: self.max_content_chars]
            warnings.append("content_truncated")
        if not text.strip():
            warnings.append("empty_text")
        try:
            latency = float(item["latency_ms"]) if item.get("latency_ms") is not None else None
        except (TypeError, ValueError):
            latency = None
        links = item.get("links") if isinstance(item.get("links"), list) else None
        title = _s(item.get("title"), 300)
        if self.mock and title and not title.startswith("[MOCK]"):
            title = "[MOCK] " + title
        return {
            "url": url, "final_url": final_url, "title": title,
            "description": _s(item.get("description"), 1000), "language": _s(item.get("language"), 20),
            "published_date": _s(item.get("published_date"), 40),
            "format": _s(item.get("format"), 20), "text": text, "text_truncated": truncated,
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "latency_ms": latency, "warnings": warnings,
            "links": [str(u)[:2048] for u in links[:200]] if links else None,
            "provenance": {"provider": FETCH_PROVIDER, "provider_mode": self.mode, "fetched_at": now()},
        }

    # ----------------------------------------------------------------- agent
    def run_public_agent(self, url, goal, output_schema=None, *, max_duration_seconds=None, max_steps=None,
                         browser_profile="lite"):
        """Synchronous, read-only TinyFish Agent run. Never retried (metered)."""
        self._require_configured()
        safe_url = url_safety.normalize_public_url(url, resolve=self.resolve_dns and not self.mock)
        if not isinstance(goal, str) or not 10 <= len(goal) <= 2000:
            raise TinyFishError("invalid_request", "The agent goal must be 10–2000 characters.", 422)
        agent_config = {"max_duration_seconds": int(max_duration_seconds
                                                    or config.TINYFISH_AGENT_MAX_DURATION_SECONDS)}
        steps = config.TINYFISH_AGENT_MAX_STEPS if max_steps is None else max_steps
        if steps:
            agent_config["max_steps"] = int(steps)
        body = {"url": safe_url, "goal": goal, "browser_profile": browser_profile if browser_profile in
                ("lite", "stealth") else "lite", "agent_config": agent_config}
        if output_schema:
            body["output_schema"] = output_schema
        data = self._call("POST", f"{self.agent_url}/v1/automation/run", body=body,
                          timeout=agent_config["max_duration_seconds"] + 30, attempts=1)
        status = str(data.get("status") or "").upper()
        if status not in ("COMPLETED", "FAILED"):
            raise TinyFishError("bad_response", "TinyFish agent response had an unknown status.", 502)
        err = data.get("error") if isinstance(data.get("error"), dict) else None
        result = data.get("result")
        if result is not None and not isinstance(result, (dict, list)):
            result = None
        return {
            "provider": AGENT_PROVIDER, "provider_mode": self.mode, "url": safe_url,
            "run_id": _s(data.get("run_id"), 80), "status": status,
            "started_at": _s(data.get("started_at"), 40), "finished_at": _s(data.get("finished_at"), 40),
            "num_of_steps": data.get("num_of_steps") if isinstance(data.get("num_of_steps"), int) else None,
            "result": result,
            "error": {"code": _s(err.get("code"), 60), "category": _s(err.get("category"), 30),
                      "message": "The TinyFish agent could not complete the extraction."} if err else None,
            "trust": "untrusted_candidate", "note": UNTRUSTED_NOTE,
        }


def get_client(**overrides):
    """Factory used by routes; tests monkeypatch this to inject a fake transport."""
    return TinyFishClient(**overrides)


def provider_status():
    """Secret-free description of the TinyFish configuration for the admin UI."""
    c = TinyFishClient()
    return {
        "configured": c.configured,
        "provider_mode": c.mode if c.configured else "not_configured",
        "api_key_present": bool(config.TINYFISH_API_KEY),
        "agent_enabled": bool(config.TINYFISH_ENABLE_AGENT),
        "agent_daily_limit": config.TINYFISH_AGENT_DAILY_LIMIT,
        "agent_max_duration_seconds": config.TINYFISH_AGENT_MAX_DURATION_SECONDS,
        "max_fetch_urls": c.max_fetch_urls,
        "max_search_results": c.max_search_results,
        "cache_ttl_seconds": c.cache_ttl,
        "cache_hits_since_start": CACHE.hits,
        "dns_check": bool(c.resolve_dns),
        "endpoints": {"search": urlsplit(c.search_url).hostname, "fetch": urlsplit(c.fetch_url).hostname,
                      "agent": urlsplit(c.agent_url).hostname},
        "message": None if c.configured else _NOT_CONFIGURED,
    }
