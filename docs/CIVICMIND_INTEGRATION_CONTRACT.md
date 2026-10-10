# CivicMind integration contract

Shared source of truth for the two parallel tracks:

- **Claude track** (`claude-platform` branch): existing application, portals, TinyFish adapter, multilingual
  civilian AI, final integration.
- **Astra track**: self-contained Utility Network Intelligence subsystem.

Part A is the locked contract from the playbook (identical in both plans). Part B records what the actual
code looks like, the mismatches found and the **proposed** resolutions. Nothing in Part B changes Part A
until the human integrator accepts it for both tracks.

Baseline commit for both tracks: `7abba6d` (`master`, 2026-10-09). Astra should branch from it, e.g.

```bash
git worktree add ../civicmind-astra -b astra-utility 7abba6d
```

---

## Part A — locked shared contract

### Product and stack

- One existing CivicMind application: FastAPI + SQLite + HTML/CSS/vanilla JavaScript + Leaflet + Chart.js.
  Preserve existing code and data. No rebuild, no framework migration.
- Three role-based portals (Civilian, Officer, Admin) over one application and one authoritative database.
  The backend is authoritative for identity, permissions and status transitions; frontend route guards are
  not security controls.
- Civilian AI targets English, Hindi and Marathi. Preserve the original citizen text; translations are stored
  separately and reviewed by the citizen before submission.
- Synthetic demo utility network first; official data only when authorized, source-attributed and reviewed.
- Light, calm UI; features and integration before final polish, with data/secret protection always active.

### AI boundaries

AI understands, classifies, translates, summarizes and drafts recommendations. Deterministic code computes
spatial distances, graph traversal, permissions, valid transitions and rule outcomes. Human operators approve
consequential actions. No AI path may execute SQL, shell commands or any utility operation.

### Canonical entities

| Entity | Minimum fields / semantics |
|---|---|
| `UtilityAsset` | `asset_id`, `asset_type`, `name`, `geometry` (GeoJSON or explicit representation), `operational_status` when known, `criticality` when sourced, `operator_org_id` when known, `source_id`, `provenance_class`, `verification_status`, `is_synthetic`, `source_updated_at`, `last_verified_at` |
| `UtilityConnection` | `connection_id`, `from_asset_id`, `to_asset_id`, `connection_type`, `direction` (`upstream` / `downstream` / `bidirectional` / `unknown`), `source_id`, `verification_status`, `provenance_class`, `valid_from`/`valid_to` when supplied, `notes`. A geometric near-match is not a connection. |
| `UtilityContact` | `contact_id`, organization, role, phone/email, emergency flag, hours/applicability, `source_id`, `last_verified_at`, `visibility_level`. Only verified contacts are operational suggestions. Synthetic contacts must be conspicuously fictional. |
| `UtilityMaintenance` | `record_id`, `asset_id`, `work_type`, date, outcome/status, work or incident reference, responsible department when known, `source_id`, evidence reference, `provenance_class` |
| `DataSource` / `ImportJob` | `source_id`, `source_type`, authority, source URL or file metadata, license/usage notes, discovered/fetched/imported timestamps, schema/CRS, status, error summary, provenance, reviewer and review timestamp |
| `NetworkTraceResult` | selected asset, direction, visited assets, explicit edges, possible sources, candidate isolation assets, supporting record IDs, warnings, unknown/missing links, trace limits, provenance summary. Never asserts an unrecorded connection. |
| `RiskFinding` | `finding_id`, `rule_id`, severity, affected asset/incident IDs, supporting record IDs, explanation, `created_at`, review status, provenance notes |

### Provenance vocabulary

Exactly: `verified_as_built`, `official_unverified`, `planned_design`, `approximate_unverified`,
`synthetic_demo`. An official document is not proof of as-built condition; a planning line is not an
installed line.

### Canonical endpoints

| Endpoint | Owner | Notes |
|---|---|---|
| `GET /api/utility/assets` | Astra service; Claude registers router | Filters: bbox, asset_types, status, provenance. GeoJSON FeatureCollection or documented envelope. |
| `GET /api/utility/assets/{asset_id}` | Astra | Role-authorized detail. Sensitive geometry and contacts are not civilian fields. |
| `GET /api/utility/assets/{asset_id}/trace?direction=upstream\|downstream\|both&max_hops=6` | Astra | Deterministic, bounded traversal; explicit unknown links. |
| `GET /api/utility/assets/{asset_id}/history` | Astra | Maintenance and incident associations by role. |
| `GET /api/incidents/{incident_id}/utility-context` | Astra service; Claude wires into incident flow | Candidate compatible assets with match reasons/distances, trace links, uncertainty; no causation claim. |
| `POST /api/utility/import/preview` | Astra | Validate only; no persistence. |
| `POST /api/utility/import/confirm` | Astra | Admin-only, idempotent persistence of an approved preview. |
| `POST /api/admin/data-sources/discover` | Claude / TinyFish | TinyFish Search → source candidates (not trusted assets). |
| `POST /api/admin/data-sources/fetch` | Claude / TinyFish | TinyFish Fetch of admin-selected public URLs; max 10 per call; content stays untrusted pending review. |
| `POST /api/admin/data-sources/agent-extract` | Claude / TinyFish (optional) | User-triggered, read-only, domain-limited, capped Agent run. |

### API behavior

- Use the app's established response conventions; document success and error shapes.
- All protected reads and writes enforce backend roles and resource access; client IDs never imply access.
- Expected HTTP status codes; structured, non-sensitive messages (no secrets, stack traces, paths or raw
  exception strings).
- Bound bbox extents, `max_hops`, pagination, search lengths, import sizes, timeouts and result counts.

### TinyFish (mandatory)

- Server-side only, `X-API-Key` header, key from `TINYFISH_API_KEY` (never in browser, logs, docs or Git).
- Admin discovery must use TinyFish Search and Fetch. Agent is optional, disabled by default, read-only,
  domain-limited and bounded.
- TinyFish is not a GIS engine: it never certifies geometry, CRS, topology, flow direction, contacts or
  isolation procedures. Results are untrusted candidate evidence requiring admin review and import validation.
- Tests mock TinyFish. Without a key the app starts and reports a clear not-configured state; it never fakes
  live output.

### Safety and merge rules

- Trace traverses only explicit recorded connections; never infers connectivity from proximity.
- Candidate isolation points are informational and evidence-backed; no valve/breaker/gas operation; isolation
  is confirmed only by an authorized operator.
- No imported data is persisted until a validated preview and an authorized admin confirmation.
- Claude owns existing shared files, TinyFish, route registration and final integration. Astra owns
  `backend/services/utility_network/`, `backend/routes/utility_network.py`, isolated utility JS/CSS
  (`frontend/js/utility-network.js`), utility tests and its handoff document.
- Separate branches/worktrees from the same baseline; no concurrent edits of one working tree; controlled
  merge; never drop/reset the existing database.

---

## Part B — verified baseline and proposed resolutions (Claude, 2026-10-10)

### B1. Verified baseline

- Startup: `cd backend && python -m uvicorn main:app --port 8000` (`run.bat` / `run.sh`). Demo:
  `run_demo.bat` builds an isolated DB at `data/demo/civicmind_demo.db`.
- Tests: `cd backend && python -m pytest` → **86 passed** at `7abba6d` (Python 3.14.3, Node 25 present).
- Roles in code: `citizen` (= Civilian portal), `officer`, `admin`. Portal pages: `citizen.html`,
  `dashboard.html` (Officer operations), `admin.html`, plus shared `map.html`, `assets.html`, `incident.html`.
  `POST /api/auth/login` returns `home` from the server-side role (`config.ROLE_HOME`).
- Auth dependencies (`backend/auth.py`): `current_user`, `require_citizen`, `require_officer`
  (officer + admin), `require_admin`. Role is re-read from the DB on every request.
- DB conventions (`backend/database.py`): `query(sql, params, one=False)`, `execute(sql, params)`,
  `with db(immediate=True) as conn:` for read-check-write transactions; UTC text timestamps
  `YYYY-MM-DD HH:MM:SS` (`database.now()`); **additive-only** schema (`SCHEMA` + `MIGRATIONS` + `INDEXES`
  applied by `init_db()` on every start; never drop/recreate).
- Error envelope: `{"detail": "text"}` or `{"detail": {"message": "text", ...extra}}`; validation errors
  `{"detail": {"message": "Invalid request.", "reasons": [...]}}`; unhandled errors return a reference id only.
- Incident ids: routes accept the numeric id **or** the public id (`INC-00042`) — see `routes/incidents.py::_find`.

### B2. Existing tables (pre-contract)

`users`, `complaints`, `incidents`, `incident_complaints`, `actions`, `evidence`, `resolutions`,
`notifications`, `activity`, `assets`, `asset_maintenance`, `complaint_assets`, `risk_findings`, `meta`.

### B3. Existing routes (pre-contract)

```
GET  /api/health  /api/config  /api/demo-info  /api/wards  /api/stream
POST /api/auth/login  /api/auth/register  /api/auth/logout-all  /api/auth/password   GET /api/auth/me
POST /api/complaints   GET /api/complaints  /api/complaints/{id}  /api/complaints/{id}/photo
GET  /api/complaints/{id}/resolution-photo  /api/complaints/{id}/plan   POST /api/complaints/{id}/resolve
GET  /api/track/{code}   GET /api/notifications   POST /api/notifications/read
GET  /api/incidents  /api/incidents/{id}   POST /api/incidents/detect  /api/incidents/{id}/investigate
POST /api/incidents/{id}/plans (legacy alias /action)  /api/incidents/{id}/verify
POST /api/actions/{id}/approve|reject|start|complete   GET /api/actions/{id}/proof
GET  /api/map/complaints  /api/map/incidents  /api/map/assets
GET  /api/dashboard  /api/dashboard/charts  /api/dashboard/citizen
GET  /api/admin/users|config|stats|complaints|resolutions   POST /api/admin/users  /api/admin/reset
GET  /api/assets/types  /api/assets  /api/assets/{id}   POST /api/assets  /api/assets/import
POST /api/assets/{id}/maintenance  /api/complaints/{id}/asset-match
GET  /api/risk/findings   POST /api/risk/scan  /api/risk/findings/{id}/review
```

No existing route collides with the canonical `/api/utility/*`, `/api/incidents/{id}/utility-context` or
`/api/admin/data-sources/*` paths.

### B4. Mismatches found and proposed resolutions (PROPOSED — needs integrator sign-off for both tracks)

| # | Mismatch | Proposed resolution |
|---|---|---|
| P1 | Tables `assets`, `asset_maintenance` and `risk_findings` **already exist** with a different shape (point registry for streetlights/roads/drains; findings keyed by `finding_key`). Creating contract tables with those names would collide. | Astra prefixes every new table with `utility_` (`utility_assets`, `utility_connections`, `utility_contacts`, `utility_maintenance`, `utility_data_sources`, `utility_import_jobs`, `utility_risk_findings`). The legacy `assets` registry stays as is; no rename. |
| P2 | Existing `risk_findings` uses `id`/`detected_at`; contract `RiskFinding` uses `finding_id`/`created_at`. | Utility rule findings live in `utility_risk_findings` with contract field names. Existing findings keep their shape; the API maps `id → finding_id`, `detected_at → created_at` only if a combined view is ever built. |
| P3 | Legacy records have `is_synthetic` + free-text `data_source`, no `provenance_class`. | No change to legacy tables. All new utility tables carry `provenance_class` from the locked vocabulary. |
| P4 | Legacy `POST /api/assets/import` persists GeoJSON points immediately (no preview). Predates the contract; touches only the legacy registry, never utility tables. | Keep for backward compatibility (documented gap). All utility imports go through `/api/utility/import/preview` → `/confirm`. |
| P5 | The contract does not name Python entry points needed for the merge. | Astra exposes (a) `routes/utility_network.py` with `router: APIRouter` (no app-level side effects at import), (b) an idempotent `init_schema(conn)` callable that only issues `CREATE TABLE/INDEX IF NOT EXISTS` and additive `ALTER TABLE ... ADD COLUMN`, (c) an optional idempotent synthetic seed function that writes only `synthetic_demo` rows, (d) a service function for import preview that accepts a `source` metadata dict (see B6). Claude calls (b) from `database.init_db()` and registers (a) in `main.py`. |
| P6 | Role naming: contract says "Civilian"; code says `citizen`. | API and DB keep `citizen`; UI text says "Civilian"/"Citizen". Astra uses `auth.require_officer` / `auth.require_admin` / `auth.current_user` — no new auth code. |
| P7 | `incident_id` path parameter. | `GET /api/incidents/{incident_id}/utility-context` accepts numeric id or `INC-xxxxx`, mirroring `routes/incidents.py::_find`. |
| P8 | Agent `max_steps` is **beta-gated** in current TinyFish docs (non-beta accounts get `403 FORBIDDEN`). | Bound Agent runs with `agent_config.max_duration_seconds` (not gated); send `max_steps` only if `TINYFISH_AGENT_MAX_STEPS` is set explicitly. |
| P9 | Search is `GET` with query parameters (the plan's flow implied a body). | Adapter uses `GET https://api.search.tinyfish.ai?query=...`; documented below. |

### B5. TinyFish API facts verified against docs.tinyfish.ai on 2026-10-10

| Capability | Request | Response used | Limits |
|---|---|---|---|
| Search | `GET https://api.search.tinyfish.ai` params `query` (required), `location`, `language`, `include_domains`, `exclude_domains` (comma-separated), `page` (0–10), `purpose` (≤2000) | `{query, results[{position, site_name, title, snippet, url, date?}], total_results, page}` | 30 req/min default; 12,000/day free, then wallet (402 `INSUFFICIENT_CREDITS`) |
| Fetch | `POST https://api.fetch.tinyfish.ai` JSON `{urls[≤10], format: markdown\|html\|json, links, ttl, per_url_timeout_ms (1–110000), purpose}` | `{results[{url, final_url, title, description, language, author, published_date, text, latency_ms, format, links?}], errors[{url, error, status?}]}` — per-URL failures arrive inside HTTP 200 | 150 URLs/min; 1,000 successful URLs/day free; batch ceiling 120 s (client timeout ≥150 s recommended) |
| Agent | `POST https://agent.tinyfish.ai/v1/automation/run` JSON `{url, goal, browser_profile: lite\|stealth, agent_config{max_duration_seconds, max_steps (beta)}, output_schema}` | `{run_id, status: COMPLETED\|FAILED, started_at, finished_at, num_of_steps, result, error{code, message, category, retry_after}}` | Metered; sync runs cannot be cancelled |
| Errors | `{"error": {"code", "message"}, "request_id"?}` | 401 `MISSING_API_KEY` / `INVALID_API_KEY`; 402 `INSUFFICIENT_CREDITS` / `FREE_ALLOWANCE_EXHAUSTED`; 429 `RATE_LIMIT_EXCEEDED`; 5xx `BACKEND_UNAVAILABLE` / `SERVICE_BUSY` (retryable) | |

Fetch per-URL error codes: `target_http_error`, `page_not_found`, `target_unreachable`, `timeout`,
`bot_blocked`, `empty_content`, `login_required`, `content_too_large`, `invalid_url`,
`invalid_redirect_url`, `proxy_error`, `conditional_unsupported`, `selector_not_matched`,
`selector_unsupported`.

### B6. Claude-owned additions on `claude-platform`

Tables (additive, in `backend/database.py`) — candidate evidence only, **never** utility assets:

- `source_discovery_runs` — one TinyFish Search per admin request (authority, region, topic, data types,
  executed query, provider mode, status, error code, result count, actor, timestamp).
- `source_candidates` — normalized search results (url, title, snippet, site, result date, position,
  discovery query, provenance JSON, `review_status` = `candidate|selected|rejected`).
- `source_snapshots` — Fetch/Agent output kept for review (url, final url, title, extracted text (capped),
  sha256, per-URL error, warnings, provider mode, `review_status` =
  `pending_review|reviewed|rejected`, reviewer, review note).
- `source_agent_runs` — Agent usage ledger for the daily cap.
- `incident_operator_updates` — officer-recorded operator coordination steps (see B7).
- Columns: `complaints.original_language`, `complaints.detected_language`,
  `complaints.description_translation_en`, `complaints.translation_engine`, `complaints.assistant_engine`;
  `users.preferred_language`.

Routes:

```
GET  /api/admin/data-sources/status
POST /api/admin/data-sources/discover            (contract)
GET  /api/admin/data-sources/candidates
POST /api/admin/data-sources/candidates/{id}/review
POST /api/admin/data-sources/fetch               (contract)
GET  /api/admin/data-sources/snapshots
GET  /api/admin/data-sources/snapshots/{id}
POST /api/admin/data-sources/snapshots/{id}/review
GET  /api/admin/data-sources/snapshots/{id}/source-metadata   (DataSource payload for Astra's preview)
POST /api/admin/data-sources/agent-extract       (contract; disabled by default)
GET  /api/assistant/capabilities
POST /api/assistant/complaint                    (citizen; suggestion only, no persistence)
POST /api/auth/preferences                       (preferred_language)
GET  /api/incidents/{id}/operator-updates   POST /api/incidents/{id}/operator-updates
GET  /citizen  /officer  /admin  /login          (redirects to the existing pages)
```

Handoff from TinyFish to Astra's importer: `GET /api/admin/data-sources/snapshots/{id}/source-metadata`
returns a `DataSource` dict for a **reviewed** snapshot only:

```json
{
  "source_type": "web_page",
  "authority": "Pune Municipal Corporation",
  "source_url": "https://...", "final_url": "https://...",
  "title": "...", "fetched_at": "2026-10-10 09:12:00",
  "discovered_via": "tinyfish_search", "discovery_query": "...",
  "content_sha256": "...", "license_notes": null,
  "provenance_class": "official_unverified",
  "review": {"status": "reviewed", "reviewer_id": 3, "reviewed_at": "...", "note": "..."},
  "snapshot_id": 12, "candidate_id": 40
}
```

`provenance_class` is `official_unverified` at best: a web page is never `verified_as_built`. Astra's preview
must still validate geometry/CRS/IDs and the admin must still confirm.

### B7. Water-leak coordination (Claude-owned, workflow side)

`incident_operator_updates` records human-entered steps: `operator_contacted`,
`operator_guidance_received`, `isolation_confirmed_by_operator`, `isolation_not_required`,
`crew_dispatched`. Deterministic prerequisites: isolation steps require a prior `operator_contacted`
record. Only an officer/admin can write them; no AI path writes them; the app never operates a valve.
After the Astra merge, `contact_ref` may carry a `UtilityContact.contact_id` and `asset_ref` a
`UtilityAsset.asset_id`.

### B7a. Utility subsystem built on `claude-platform` (2026-10-10, owner instruction)

The owner's later master prompt asked Claude to implement the Utility Network Intelligence subsystem directly.
It now exists on `claude-platform` at the Astra-owned paths named in Part A (`backend/services/utility_network/`,
`backend/routes/utility_network.py`, `frontend/js/utility-network.js`, `backend/tests/test_utility_network.py`)
and follows Part A plus proposals P1, P5 and P7:

- Tables `utility_assets`, `utility_connections`, `utility_contacts`, `utility_maintenance`, `utility_data_sources`,
  `utility_import_jobs`, `utility_findings`, `incident_utility_assets` via idempotent `init_schema(conn)`.
- Canonical endpoints implemented as written: `GET /api/utility/assets[/{asset_id}[/trace|/history]]`,
  `GET /api/incidents/{incident_id}/utility-context`, `POST /api/utility/import/preview|confirm`.
  Additions: `/api/utility/meta`, `/assets/{id}/isolation`, `/assets/{id}/contacts`, `/assets/{id}/copilot`,
  `/api/utility/findings*`, `/api/utility/import/jobs`, `/api/utility/records/verify`,
  `/api/incidents/{id}/utility-assets/{asset_id}/review`, `/api/incidents/{id}/briefing`.
- `max_hops` default is 10 (cap 25) rather than the example 6: the synthetic source is 7 hops from the demo leak,
  and real distribution networks are deeper. Reaching the limit is always reported.
- Point assets act as network nodes (no separate nodes table); `asset_id` is the public string identifier.

If an Astra branch is still delivered, compare it against this implementation before merging; do not merge two
utility subsystems.

### B8. Astra handoff checklist (reviewed by Claude at C2)

- [ ] Only Astra-owned paths changed (`backend/services/utility_network/`, `backend/routes/utility_network.py`,
      `frontend/js/utility-network.js`, utility CSS, `backend/tests/test_utility_*.py`, handoff doc, demo data file).
- [ ] No edits to `main.py`, `database.py`, `config.py`, `auth.py` or existing pages.
- [ ] Tables prefixed `utility_` (P1) and created by an idempotent `init_schema(conn)` (P5).
- [ ] Entity fields, provenance vocabulary and endpoint paths match Part A.
- [ ] Trace uses explicit connections only; bounded `max_hops`/result count; unknown links reported.
- [ ] Civilian role never receives gas/electric topology or operator contacts.
- [ ] Synthetic contacts are visibly fictional; seed is idempotent and `synthetic_demo` only.
- [ ] Preview never persists; confirm is admin-only and idempotent.
- [ ] Tests run against a temporary DB and do not call external services.
