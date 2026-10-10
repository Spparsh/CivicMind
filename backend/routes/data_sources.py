"""CivicMind — admin public data-source discovery (TinyFish Search / Fetch / optional Agent).

Every route requires the admin role (router-level dependency, reads included).

Flow:  discover (Search) -> admin selects candidates -> fetch (Fetch, <=10 URLs)
       -> admin reviews the extracted content -> source metadata handed to the
       utility import PREVIEW -> admin confirms the import there.

Nothing in this module writes infrastructure records. Search results are
candidates; fetched text is untrusted evidence kept for human review.

  GET  /api/admin/data-sources/status
  POST /api/admin/data-sources/discover
  GET  /api/admin/data-sources/candidates
  POST /api/admin/data-sources/candidates/{id}/review
  POST /api/admin/data-sources/fetch
  GET  /api/admin/data-sources/snapshots
  GET  /api/admin/data-sources/snapshots/{id}
  POST /api/admin/data-sources/snapshots/{id}/review
  GET  /api/admin/data-sources/snapshots/{id}/source-metadata
  POST /api/admin/data-sources/agent-extract        (disabled unless TINYFISH_ENABLE_AGENT=true)
"""
import hashlib
import importlib.util
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import config
from auth import require_admin
from database import query, db, now
from routes.common import safe_json
from services.tinyfish import client as tf
from services.tinyfish import url_safety
from services.tinyfish.client import TinyFishError
from services.tinyfish.url_safety import UnsafeURL

router = APIRouter(prefix="/api/admin/data-sources", tags=["data-sources"],
                   dependencies=[Depends(require_admin)])

DATA_TYPES = {
    "gis_layer": "GIS map layer",
    "open_data": "open data portal dataset",
    "network_plan": "water supply network plan",
    "contact_directory": "department contact directory",
    "maintenance_notice": "maintenance or shutdown notice",
    "works_document": "works tender document",
}
SEARCH_PURPOSE = ("Find official, publicly available municipal infrastructure data sources (GIS layers, "
                  "open data, network plans, department contacts) for a civic platform administrator to review.")
AGENT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "value": {"type": "string"}, "source_url": {"type": "string"}}}},
        "notes": {"type": "string"},
    },
    "required": ["items"],
}
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]{3,253}$")


# --------------------------------------------------------------------------- helpers
def _raise_tf(e: TinyFishError):
    raise HTTPException(e.http_status, {"message": e.message, "code": e.code, "retryable": e.retryable})


def _clean(val, limit):
    return " ".join(str(val or "").split())[:limit]


def _log(conn, admin, action, detail):
    conn.execute("INSERT INTO activity (actor, actor_id, action, detail, created_at) VALUES (?,?,?,?,?)",
                 (admin["name"], admin["id"], action, detail[:300], now()))


def _domains(raw):
    out = []
    for d in raw or []:
        d = str(d).strip().lower()
        d = re.sub(r"^https?://", "", d).split("/")[0].lstrip(".")
        if not d:
            continue
        if not _DOMAIN_RE.match(d):
            raise HTTPException(422, {"message": f"'{d[:60]}' is not a valid domain.", "code": "invalid_domain"})
        try:
            url_safety.normalize_public_url(f"https://{d}/", resolve=False)
        except UnsafeURL as e:
            raise HTTPException(422, {"message": f"Domain '{d[:60]}' is not allowed: {e.message}",
                                      "code": e.code}) from None
        out.append(d)
    return out[:10]


def utility_import_available():
    """True once Astra's utility network module has been merged and registered."""
    try:
        return importlib.util.find_spec("routes.utility_network") is not None
    except (ImportError, ValueError):
        return False


def _candidate_out(r):
    return {k: r.get(k) for k in ("id", "run_id", "url", "title", "snippet", "site_name", "result_date",
                                  "position", "review_status", "reviewed_at", "created_at")} | {
        "provenance": safe_json(r.get("provenance"), {})}


def _snapshot_summary(r):
    text = r.get("content_text") or ""
    return {k: r.get(k) for k in ("id", "candidate_id", "method", "url", "final_url", "title", "language",
                                  "published_date", "content_format", "content_chars", "fetch_status",
                                  "error_code", "error_status", "provider_mode", "fetched_at",
                                  "review_status", "reviewed_at", "review_note", "run_ref")} | {
        "content_truncated": bool(r.get("content_truncated")),
        "warnings": safe_json(r.get("warnings"), []) or [],
        "preview": text[:280],
        "trust": "untrusted_candidate",
    }


def _today_count(table, extra=""):
    return query(f"SELECT COUNT(*) c FROM {table} WHERE created_at >= date('now') {extra}", one=True)["c"]


# --------------------------------------------------------------------------- status
@router.get("/status")
def status():
    st = tf.provider_status()
    st["usage_today"] = {
        "discovery_runs": _today_count("source_discovery_runs"),
        "fetched_urls": query("SELECT COUNT(*) c FROM source_snapshots WHERE method='tinyfish_fetch' "
                              "AND fetched_at >= date('now')", one=True)["c"],
        "agent_runs": _today_count("source_agent_runs"),
    }
    st["utility_import_available"] = utility_import_available()
    st["data_types"] = [{"key": k, "label": v} for k, v in DATA_TYPES.items()]
    st["untrusted_note"] = tf.UNTRUSTED_NOTE
    return st


# --------------------------------------------------------------------------- discover
class DiscoverIn(BaseModel):
    authority: str = Field(min_length=2, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    topic: str = Field(min_length=2, max_length=160)
    data_types: list[str] = Field(default_factory=list, max_length=6)
    include_domains: list[str] = Field(default_factory=list, max_length=10)
    page: int = Field(default=0, ge=0, le=10)


@router.post("/discover")
def discover(body: DiscoverIn, admin: dict = Depends(require_admin)):
    bad = [t for t in body.data_types if t not in DATA_TYPES]
    if bad:
        raise HTTPException(422, {"message": "Unknown data type.", "code": "invalid_data_type",
                                  "allowed": list(DATA_TYPES)})
    authority, region, topic = _clean(body.authority, 120), _clean(body.region, 120), _clean(body.topic, 160)
    domains = _domains(body.include_domains)
    q = " ".join(p for p in [authority, region, topic] + [DATA_TYPES[t] for t in body.data_types] if p)[:400]
    client = tf.get_client()
    try:
        res = client.search_sources(q, include_domains=domains, page=body.page, purpose=SEARCH_PURPOSE,
                                    location="IN")
    except TinyFishError as e:
        if e.code != "not_configured":
            with db() as conn:
                conn.execute("""INSERT INTO source_discovery_runs (authority, region, topic, data_types, query,
                                provider, provider_mode, status, error_code, requested_by, created_at)
                                VALUES (?,?,?,?,?,?,?, 'failed', ?, ?, ?)""",
                             (authority, region or None, topic, json.dumps(body.data_types), q,
                              tf.SEARCH_PROVIDER, client.mode, e.code, admin["id"], now()))
        _raise_tf(e)
    with db(immediate=True) as conn:
        cur = conn.execute("""INSERT INTO source_discovery_runs (authority, region, topic, data_types, query,
                              provider, provider_mode, status, result_count, requested_by, created_at)
                              VALUES (?,?,?,?,?,?,?, 'completed', ?, ?, ?)""",
                           (authority, region or None, topic, json.dumps(body.data_types), res["query"],
                            tf.SEARCH_PROVIDER, res["provider_mode"], len(res["candidates"]), admin["id"], now()))
        run_id = cur.lastrowid
        for c in res["candidates"]:
            conn.execute("""INSERT OR IGNORE INTO source_candidates (run_id, url, title, snippet, site_name,
                            result_date, position, discovery_query, provenance, created_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (run_id, c["url"], c["title"], c["snippet"], c["site_name"], c["result_date"],
                          c["position"], res["query"], json.dumps(c["provenance"]), now()))
        _log(conn, admin, "data_source_discovery",
             f"TinyFish Search ({res['provider_mode']}): {len(res['candidates'])} candidate(s) for '{q[:120]}'")
    run = query("SELECT * FROM source_discovery_runs WHERE id=?", (run_id,), one=True)
    rows = query("SELECT * FROM source_candidates WHERE run_id=? ORDER BY position, id", (run_id,))
    return {"run": run | {"data_types": safe_json(run["data_types"], [])},
            "candidates": [_candidate_out(r) for r in rows], "provider_mode": res["provider_mode"],
            "dropped": res["dropped"], "cached": res.get("cached", False), "trust": "untrusted_candidate",
            "note": tf.UNTRUSTED_NOTE}


@router.get("/candidates")
def candidates(run_id: int | None = None,
               status: str | None = Query(None, pattern="^(candidate|selected|rejected)$"),
               limit: int = Query(100, ge=1, le=300)):
    sql, params = "SELECT * FROM source_candidates WHERE 1=1", []
    if run_id:
        sql += " AND run_id=?"
        params.append(run_id)
    if status:
        sql += " AND review_status=?"
        params.append(status)
    order = " ORDER BY position, id" if run_id else " ORDER BY id DESC"
    rows = query(sql + order + " LIMIT ?", tuple(params + [limit]))
    runs = query("SELECT id, authority, region, topic, query, provider_mode, status, error_code, result_count, "
                 "created_at FROM source_discovery_runs ORDER BY id DESC LIMIT 20")
    return {"candidates": [_candidate_out(r) for r in rows], "runs": runs}


class CandidateReviewIn(BaseModel):
    status: str = Field(pattern="^(candidate|selected|rejected)$")


@router.post("/candidates/{candidate_id}/review")
def review_candidate(candidate_id: int, body: CandidateReviewIn, admin: dict = Depends(require_admin)):
    with db(immediate=True) as conn:
        cur = conn.execute("UPDATE source_candidates SET review_status=?, reviewed_by=?, reviewed_at=? WHERE id=?",
                           (body.status, admin["id"], now(), candidate_id))
        if cur.rowcount != 1:
            raise HTTPException(404, "Candidate not found")
    return {"ok": True, "status": body.status}


# --------------------------------------------------------------------------- fetch
class FetchIn(BaseModel):
    candidate_ids: list[int] = Field(default_factory=list, max_length=10)
    urls: list[str] = Field(default_factory=list, max_length=10)
    format: str = Field(default="markdown", pattern="^(markdown|html)$")


def _store_snapshot(conn, admin, method, mode, candidate_id, url, res=None, err=None, run_ref=None):
    if res is not None:
        text = res.get("text") or ""
        cur = conn.execute(
            """INSERT INTO source_snapshots (candidate_id, method, url, final_url, title, description, language,
               published_date, content_format, content_text, content_chars, content_truncated, content_sha256,
               fetch_status, warnings, provider_mode, run_ref, requested_by, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'ok', ?,?,?,?,?)""",
            (candidate_id, method, url, res.get("final_url"), res.get("title"), res.get("description"),
             res.get("language"), res.get("published_date"), res.get("format"), text, len(text),
             1 if res.get("text_truncated") else 0, res.get("content_sha256"),
             json.dumps(res.get("warnings") or []), mode, run_ref, admin["id"], now()))
    else:
        cur = conn.execute(
            """INSERT INTO source_snapshots (candidate_id, method, url, fetch_status, error_code, error_status,
               warnings, provider_mode, run_ref, requested_by, fetched_at, review_status)
               VALUES (?,?,?, 'error', ?,?, '[]', ?,?,?,?, 'rejected')""",
            (candidate_id, method, url, err.get("error"), err.get("status"), mode, run_ref, admin["id"], now()))
    return cur.lastrowid


@router.post("/fetch")
def fetch(body: FetchIn, admin: dict = Depends(require_admin)):
    client = tf.get_client()
    if len(body.candidate_ids) + len(body.urls) == 0:
        raise HTTPException(422, {"message": "Select at least one candidate or URL.", "code": "invalid_request"})
    if len(body.candidate_ids) + len(body.urls) > client.max_fetch_urls:
        raise HTTPException(422, {"message": f"Fetch at most {client.max_fetch_urls} URLs at a time.",
                                  "code": "too_many_urls"})
    url_to_candidate, urls = {}, []
    for cid in dict.fromkeys(body.candidate_ids):
        c = query("SELECT id, url, review_status FROM source_candidates WHERE id=?", (cid,), one=True)
        if not c:
            raise HTTPException(404, {"message": f"Candidate {cid} not found.", "code": "not_found"})
        if c["review_status"] == "rejected":
            raise HTTPException(409, {"message": f"Candidate {cid} was rejected; re-select it first.",
                                      "code": "candidate_rejected"})
        urls.append(c["url"])
        url_to_candidate[c["url"]] = c["id"]
    urls += [str(u) for u in body.urls]
    try:
        res = client.fetch_sources(urls, fmt=body.format, links=False, purpose=SEARCH_PURPOSE)
    except TinyFishError as e:
        _raise_tf(e)
    stored, errors = [], []
    with db(immediate=True) as conn:
        for r in res["results"]:
            cid = url_to_candidate.get(r["url"])
            sid = _store_snapshot(conn, admin, "tinyfish_fetch", res["provider_mode"], cid, r["url"], res=r)
            stored.append(sid)
            if cid:
                conn.execute("UPDATE source_candidates SET review_status='selected', reviewed_by=?, reviewed_at=? "
                             "WHERE id=? AND review_status='candidate'", (admin["id"], now(), cid))
        for e in res["errors"]:
            entry = {k: e.get(k) for k in ("url", "error", "status", "message", "stage")}
            # URLs refused by the safety layer were never sent to TinyFish; record provider failures only.
            if e.get("stage") != "safety_check":
                entry["snapshot_id"] = _store_snapshot(conn, admin, "tinyfish_fetch", res["provider_mode"],
                                                       url_to_candidate.get(e.get("url")), e.get("url"), err=e)
            errors.append(entry)
        _log(conn, admin, "data_source_fetch",
             f"TinyFish Fetch ({res['provider_mode']}): {len(stored)} page(s) stored for review, "
             f"{len(errors)} error(s)")
    rows = [query("SELECT * FROM source_snapshots WHERE id=?", (sid,), one=True) for sid in stored]
    return {"provider_mode": res["provider_mode"], "snapshots": [_snapshot_summary(r) for r in rows],
            "errors": errors, "cache_hits": res.get("cache_hits", 0), "trust": "untrusted_candidate",
            "note": tf.UNTRUSTED_NOTE}


# --------------------------------------------------------------------------- snapshots / review
@router.get("/snapshots")
def snapshots(review_status: str | None = Query(None, pattern="^(pending_review|reviewed|rejected)$"),
              limit: int = Query(100, ge=1, le=300)):
    sql, params = "SELECT * FROM source_snapshots", []
    if review_status:
        sql += " WHERE review_status=?"
        params.append(review_status)
    return [_snapshot_summary(r) for r in query(sql + " ORDER BY id DESC LIMIT ?", tuple(params + [limit]))]


def _snapshot(snapshot_id):
    r = query("SELECT * FROM source_snapshots WHERE id=?", (snapshot_id,), one=True)
    if not r:
        raise HTTPException(404, "Snapshot not found")
    return r


@router.get("/snapshots/{snapshot_id}")
def snapshot_detail(snapshot_id: int):
    r = _snapshot(snapshot_id)
    out = _snapshot_summary(r) | {"description": r.get("description"), "content_text": r.get("content_text"),
                                  "content_sha256": r.get("content_sha256"), "note": tf.UNTRUSTED_NOTE}
    out["candidate"] = None
    if r.get("candidate_id"):
        c = query("""SELECT c.*, d.authority, d.region, d.topic FROM source_candidates c
                     JOIN source_discovery_runs d ON d.id=c.run_id WHERE c.id=?""", (r["candidate_id"],), one=True)
        if c:
            out["candidate"] = _candidate_out(c) | {"authority": c["authority"], "region": c["region"],
                                                    "topic": c["topic"], "discovery_query": c["discovery_query"]}
    return out


class SnapshotReviewIn(BaseModel):
    decision: str = Field(pattern="^(reviewed|rejected|pending_review)$")
    note: str | None = Field(default=None, max_length=500)


@router.post("/snapshots/{snapshot_id}/review")
def review_snapshot(snapshot_id: int, body: SnapshotReviewIn, admin: dict = Depends(require_admin)):
    r = _snapshot(snapshot_id)
    if body.decision == "reviewed" and r["fetch_status"] != "ok":
        raise HTTPException(409, {"message": "A failed fetch has no content to approve.", "code": "no_content"})
    with db(immediate=True) as conn:
        conn.execute("""UPDATE source_snapshots SET review_status=?, reviewed_by=?, reviewed_at=?, review_note=?
                        WHERE id=?""", (body.decision, admin["id"], now(), (body.note or "").strip() or None,
                                        snapshot_id))
        _log(conn, admin, "data_source_reviewed", f"Source snapshot #{snapshot_id} marked {body.decision}")
    return _snapshot_summary(_snapshot(snapshot_id))


@router.get("/snapshots/{snapshot_id}/source-metadata")
def source_metadata(snapshot_id: int):
    """DataSource metadata for the utility import PREVIEW (contract section B6).

    Only for content an admin has reviewed. It describes where data came from;
    it is not data. Records still go through the import preview's validation
    and an explicit admin confirmation before anything is persisted."""
    r = _snapshot(snapshot_id)
    if r["fetch_status"] != "ok" or r["review_status"] != "reviewed":
        raise HTTPException(409, {"message": "Review and approve this snapshot before using it as an import source.",
                                  "code": "not_reviewed"})
    cand = query("""SELECT c.id, c.discovery_query, d.authority FROM source_candidates c
                    JOIN source_discovery_runs d ON d.id=c.run_id WHERE c.id=?""",
                 (r["candidate_id"],), one=True) if r.get("candidate_id") else None
    return {
        "source_type": "web_page" if r["method"] == "tinyfish_fetch" else "agent_extraction",
        "authority": cand["authority"] if cand else None,
        "source_url": r["url"], "final_url": r["final_url"], "title": r["title"],
        "fetched_at": r["fetched_at"],
        "discovered_via": "tinyfish_search" if cand else "admin_entered_url",
        "discovery_query": cand["discovery_query"] if cand else None,
        "content_sha256": r["content_sha256"], "license_notes": None,
        # A web page is at best official-but-unverified; mock content is never official.
        "provenance_class": "synthetic_demo" if r["provider_mode"] == "mock" else "official_unverified",
        "provider_mode": r["provider_mode"],
        "review": {"status": r["review_status"], "reviewer_id": r["reviewed_by"], "reviewed_at": r["reviewed_at"],
                   "note": r["review_note"]},
        "snapshot_id": r["id"], "candidate_id": cand["id"] if cand else None,
        "utility_import_available": utility_import_available(),
        "next_step": ("Send this metadata with the reviewed records to the utility import preview."
                      if utility_import_available() else
                      "The utility import module is not installed yet; keep this source for the import step."),
    }


# --------------------------------------------------------------------------- agent (optional)
class AgentIn(BaseModel):
    url: str = Field(max_length=2048)
    extract: str = Field(min_length=10, max_length=300)


def agent_goal(url, host, extract):
    return (f"Read-only task on the public page {url}. {extract} "
            f"Rules: only read publicly visible content; stay on {host} and its subdomains; do not log in, "
            "sign up, fill or submit any form, accept terms, download files, or click buttons that change "
            "anything. If the page needs a login or a CAPTCHA, stop and say so in notes. Return only the "
            "requested information as JSON matching the output schema, with the URL each item came from.")


@router.post("/agent-extract")
def agent_extract(body: AgentIn, admin: dict = Depends(require_admin)):
    if not config.TINYFISH_ENABLE_AGENT:
        raise HTTPException(403, {"message": "TinyFish Agent extraction is disabled. Enable it with "
                                  "TINYFISH_ENABLE_AGENT=true after checking TinyFish pricing and limits.",
                                  "code": "agent_disabled"})
    client = tf.get_client()
    if not client.configured:
        _raise_tf(TinyFishError("not_configured", "TinyFish is not configured.", 503))
    try:
        url = url_safety.normalize_public_url(body.url, resolve=client.resolve_dns and not client.mock)
    except UnsafeURL as e:
        raise HTTPException(422, {"message": e.message, "code": e.code}) from None
    host = url_safety.host_of(url)
    selected_hosts = {url_safety.host_of(r["url"]) for r in
                      query("SELECT url FROM source_candidates WHERE review_status='selected'")}
    if not (any(url_safety.same_site(host, h) for h in selected_hosts)
            or url_safety.domain_allowed(host, config.TINYFISH_AGENT_ALLOWED_DOMAINS)):
        raise HTTPException(403, {"message": "The Agent may only visit domains of selected source candidates "
                                  "or TINYFISH_AGENT_ALLOWED_DOMAINS.", "code": "domain_not_allowed"})
    used = _today_count("source_agent_runs")
    if used >= config.TINYFISH_AGENT_DAILY_LIMIT:
        raise HTTPException(429, {"message": f"Daily Agent limit reached ({config.TINYFISH_AGENT_DAILY_LIMIT}).",
                                  "code": "agent_daily_limit"})
    extract = _clean(body.extract, 300)
    try:
        res = client.run_public_agent(url, agent_goal(url, host, extract), AGENT_OUTPUT_SCHEMA)
    except (TinyFishError, UnsafeURL) as e:
        code = getattr(e, "code", "agent_failed")
        with db() as conn:
            conn.execute("INSERT INTO source_agent_runs (url, status, error_code, provider_mode, requested_by, "
                         "created_at) VALUES (?, 'ERROR', ?, ?, ?, ?)", (url, code, client.mode, admin["id"], now()))
        if isinstance(e, UnsafeURL):
            raise HTTPException(422, {"message": e.message, "code": e.code}) from None
        _raise_tf(e)
    text = json.dumps(res["result"], ensure_ascii=False, indent=1) if res["result"] is not None else ""
    with db(immediate=True) as conn:
        conn.execute("INSERT INTO source_agent_runs (url, status, run_ref, error_code, provider_mode, requested_by, "
                     "created_at) VALUES (?,?,?,?,?,?,?)",
                     (url, res["status"], res["run_id"], (res["error"] or {}).get("code"), res["provider_mode"],
                      admin["id"], now()))
        if res["status"] == "COMPLETED" and text:
            snap = {"final_url": url, "title": f"Agent extraction: {extract[:120]}", "format": "json", "text": text,
                    "text_truncated": False, "warnings": ["agent_output_unverified"],
                    "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
            sid = _store_snapshot(conn, admin, "tinyfish_agent", res["provider_mode"], None, url, res=snap,
                                  run_ref=res["run_id"])
        else:
            sid = _store_snapshot(conn, admin, "tinyfish_agent", res["provider_mode"], None, url,
                                  err={"error": (res["error"] or {}).get("code") or "agent_failed"},
                                  run_ref=res["run_id"])
        _log(conn, admin, "data_source_agent", f"TinyFish Agent ({res['provider_mode']}) on {host}: {res['status']}")
    return {"run": {k: res[k] for k in ("run_id", "status", "num_of_steps", "provider_mode")} | {
                "error": res["error"]},
            "snapshot": _snapshot_summary(_snapshot(sid)),
            "usage": {"used_today": used + 1, "daily_limit": config.TINYFISH_AGENT_DAILY_LIMIT},
            "note": tf.UNTRUSTED_NOTE}
