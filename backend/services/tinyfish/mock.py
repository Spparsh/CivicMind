"""
CivicMind — TinyFish mock transport (TINYFISH_MOCK_MODE=true; local demos and tests only).

Responses follow the documented TinyFish shapes so the whole pipeline can be
exercised without a key. Every title is prefixed "[MOCK]", URLs point at the
reserved example.org domain, and the client labels all output
`provider_mode: "mock"`. Nothing here is ever presented as live data.
"""
import re


class _Resp:
    def __init__(self, status_code, data):
        self.status_code, self._data, self.headers = status_code, data, {}

    def json(self):
        return self._data


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or "source"


class MockSession:
    def request(self, method, url, params=None, json=None, headers=None, timeout=None, allow_redirects=True):
        if method == "GET" and "search" in url:
            q = (params or {}).get("query", "")
            slug = _slug(q)
            kinds = [("open-data-portal", "Open data portal — water supply network layers"),
                     ("gis-viewer", "Public GIS viewer — utility maps"),
                     ("contacts", "Department contact directory")]
            return _Resp(200, {"query": q, "page": (params or {}).get("page", 0), "total_results": len(kinds),
                               "results": [{"position": i + 1, "site_name": "example.org",
                                            "title": f"[MOCK] {label}",
                                            "snippet": f"Fictional mock result for '{q}'. Not a real authority page.",
                                            "url": f"https://example.org/civicmind-mock/{slug}/{key}",
                                            "date": "2026-01-01"} for i, (key, label) in enumerate(kinds)]})
        if method == "POST" and "fetch" in url:
            results, errors = [], []
            for u in (json or {}).get("urls", []):
                if u.rstrip("/").endswith("/missing"):
                    errors.append({"url": u, "error": "page_not_found", "status": 404})
                    continue
                results.append({"url": u, "final_url": u, "title": "[MOCK] Fetched page",
                                "description": "Fictional mock content", "language": "en", "author": None,
                                "published_date": None, "latency_ms": 5, "format": (json or {}).get("format"),
                                "text": ("# [MOCK] Fictional source page\n\nThis text was produced by "
                                         "CivicMind's TinyFish mock transport. It is not real data and "
                                         "must not be imported as infrastructure records.\n")})
            return _Resp(200, {"results": results, "errors": errors})
        if method == "POST" and "automation" in url:
            return _Resp(200, {"run_id": "mock-run", "status": "COMPLETED", "started_at": None,
                               "finished_at": None, "num_of_steps": 1,
                               "result": {"mock": True, "note": "Fictional mock extraction result."},
                               "error": None})
        return _Resp(404, {"error": {"code": "UNKNOWN_ROUTE", "message": "mock route"}})
