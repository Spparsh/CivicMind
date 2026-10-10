# CivicMind — security model and route authorisation matrix

All checks are enforced on the server by FastAPI dependencies in `backend/auth.py`
(`current_user`, `require_citizen`, `require_officer`, `require_admin`) and by ownership checks in the
routes. The frontend only mirrors these rules for usability.

Legend: **C** citizen · **O** officer · **A** admin · **own** = only the caller's records · 404 is returned
instead of 403 where revealing existence would leak information.

| Method | Path | Who | Notes / state checks | Audit |
|---|---|---|---|---|
| POST | `/api/auth/register` | public | always creates a citizen; `role` in body ignored; email + password ≥ 8 validated; throttled | — |
| POST | `/api/auth/login` | public | 5 failures / 10 min per IP+email → 429; same message for unknown email | — |
| GET | `/api/auth/me` | any | current DB role, no secrets | — |
| POST | `/api/auth/logout-all` | any | bumps token version → all tokens revoked | — |
| POST | `/api/auth/password` | any | requires current password; revokes old tokens | — |
| GET | `/api/config`, `/api/wards`, `/api/health` | public | no secrets | — |
| POST | `/api/complaints` | C | validated category/text/coords (finite, in service area); optional photo validated; duplicate guard | activity |
| GET | `/api/complaints` | C own · O · A | admins also see citizen e-mail | — |
| GET | `/api/complaints/{id}` | owner · O · A | others 404 | — |
| GET | `/api/complaints/{id}/photo`, `/resolution-photo` | owner · O · A | served from private store | — |
| GET | `/api/complaints/{id}/plan` | O · A | recommendation only | — |
| POST | `/api/complaints/{id}/resolve` | O · A | proof photo required & checked; open → resolved only (409 otherwise) | activity, notification |
| POST | `/api/complaints/{id}/asset-match` | O · A | confirm / reject an automatic match | — |
| GET | `/api/track/{code}` | public | private code only; status/category/ward/date; no PII | — |
| GET/POST | `/api/notifications`, `/read` | own | — | — |
| GET | `/api/incidents` | O · A | — | — |
| GET | `/api/incidents/{id}` | O · A full · C limited if linked | citizens: no other citizens' data, generalised location | — |
| POST | `/api/incidents/detect` | O · A | grouping run | activity |
| POST | `/api/incidents/{id}/investigate` | O · A | regenerates notes only | activity |
| POST | `/api/incidents/{id}/plans` | O · A | only if no open plan and not resolved | activity |
| POST | `/api/actions/{id}/approve` | O · A | pending → approved (edits validated) | activity, notification |
| POST | `/api/actions/{id}/reject` | O · A | pending → rejected, reason required | activity |
| POST | `/api/actions/{id}/start` | O · A | approved → in_progress | activity, notification |
| POST | `/api/actions/{id}/complete` | O · A | in_progress → completed; proof required; triggers verification | activity, notification, maintenance |
| POST | `/api/incidents/{id}/verify` | O · A | needs completed plan + accepted proof on disk; may reopen | activity, notification |
| GET | `/api/actions/{id}/proof` | O · A · linked C | private store | — |
| GET | `/api/map/complaints` | C own · O · A | — | — |
| GET | `/api/map/incidents` | C generalised · O · A | — | — |
| GET | `/api/assets`, `/api/assets/{id}`, `/api/map/assets`, `/api/assets/types` | O · A | — | — |
| POST | `/api/assets`, `/api/assets/import` | A | validated; existing codes never overwritten | activity |
| POST | `/api/assets/{id}/maintenance` | O · A | validated, past dates only | — |
| GET | `/api/risk/findings` | O · A | — | — |
| POST | `/api/risk/scan`, `/api/risk/findings/{id}/review` | O · A | — | activity |
| GET | `/api/dashboard`, `/charts` | O · A | — | — |
| GET | `/api/dashboard/citizen` | C | own counts | — |
| ALL | `/api/admin/*` | A | router-level dependency (reads included) | user creation logged |
| POST | `/api/admin/reset` | A | demo database only + typed `RESET` | — |
| GET | `/api/stream?token=` | own | SSE; token redacted from access logs | — |
| POST | `/api/auth/preferences` | any | `preferred_language` ∈ en/hi/mr | — |
| GET | `/api/assistant/capabilities` | any | states honestly whether translation/AI/speech are available | — |
| POST | `/api/assistant/complaint` | C | suggestion only, **no persistence**; 20 calls / 10 min per user; output schema-validated | — |
| GET/POST | `/api/incidents/{id}/operator-updates` | O · A | human-recorded operator coordination; deterministic prerequisites (409); closed incidents refused | activity |
| GET | `/api/admin/data-sources/status` | A | provider mode and limits; **never** the key | — |
| POST | `/api/admin/data-sources/discover` | A | TinyFish Search; results stored as candidates only | activity |
| GET/POST | `/api/admin/data-sources/candidates[/{id}/review]` | A | — | — |
| POST | `/api/admin/data-sources/fetch` | A | ≤ 10 admin-selected URLs; URL safety layer before any request; content stored as untrusted snapshot | activity |
| GET/POST | `/api/admin/data-sources/snapshots[/{id}[/review]]` | A | failed fetches cannot be approved | activity |
| GET | `/api/admin/data-sources/snapshots/{id}/source-metadata` | A | reviewed snapshots only (409 otherwise); provenance at best `official_unverified` | — |
| POST | `/api/admin/data-sources/agent-extract` | A | **disabled by default**; domain must belong to a selected candidate or the allowlist; daily cap; read-only goal | activity |
| GET | `/citizen`, `/officer`, `/admin`, `/login` | public | redirect to the page only; every API call behind it is still authorised | — |
| GET | `/api/utility/meta`, `/api/utility/assets[/{id}[/trace|/isolation|/history|/contacts]]` | O · A | utility topology, contacts and operational details are never civilian data; bbox ≤ 1°, hops ≤ 25 | — |
| POST | `/api/utility/assets/{id}/copilot` | O · A | read-only; answers built from records; AI text rejected if it cites unknown IDs | — |
| GET | `/api/incidents/{id}/utility-context` | O · A | candidates only (category + recorded geometry within 120 m) | — |
| POST | `/api/incidents/{id}/utility-assets/{asset}/review`, `/api/incidents/{id}/briefing` | O · A | confirm / reject candidate; briefing is read-only | activity |
| GET/POST | `/api/utility/findings[/scan|/{id}/review]` | O · A | rule-based, idempotent | activity |
| POST | `/api/utility/import/preview` | A | validates only, nothing persisted; CRS must be EPSG:4326; ≤ 5000 records / 6 MB | — |
| POST | `/api/utility/import/confirm` | A | transactional, idempotent (409 on repeat), never overwrites, records stored unverified | activity |
| GET | `/api/utility/import/jobs` | A | — | — |
| POST | `/api/utility/records/verify` | A | human verification decision with a required note | activity |
| GET | `/api/admin/activity` | A | audit log (no secrets are written to it) | — |
| POST | `/api/voice` | C | WAV parsed on the server (PCM 16-bit, ≤ 15.5 s, ≤ 1.2 MB); private storage; 10 per 10 min | — |
| GET | `/api/complaints/{id}/voice` | owner · O · A | private audio, `no-store` | — |
| GET | `/api/voice/policy` | any | retention and provider disclosure | — |
| GET | `/api/geo/reverse` | any | opt-in provider; service-area check before sending; cache; 1 req/s app-wide; 6/min per user | — |
| GET | `/api/geo/search?q=` | any | ward names offline; provider only when enabled, bounded to the service area, explicit search only (no autocomplete), cached, shares the 1 req/s spacing and 6/min limit | — |
| POST | `/api/media` | C | raw bytes; type sniffed from content; photos re-encoded (EXIF/GPS removed) + blank/re-use checks; videos container-parsed (video track, declared length ≤ 30 s, MP4/MOV location tags blanked); own 30 MB request cap on this path only; 20 per 10 min | — |
| DELETE | `/api/media/{id}` | C own, unattached only | file deleted | — |
| GET | `/api/complaints/{id}/media/{mid}` | owner · O · A | private file, `no-store`; uploads are claimed once (same user, ≤ 3 per report) inside the complaint transaction | — |
| POST | `/api/assistant/chat` | C | knowledge base + caller's OWN complaints only; AI text rejected if it adds numbers/links/e-mails; throttled | — |
| GET | `/api/contractors` | O · A | contact details are staff-only | — |
| POST | `/api/admin/contractors[/{id}/status]` | A | new contractors start unverified | activity |
| GET/POST | `/api/incidents/{id}/work-orders`, `/api/work-orders[/{id}]` | O · A | one open order per plan; needs an approved plan | activity |
| POST | `/api/work-orders/{id}/start|progress|evidence|submit|verify|cancel` | O · A | state checked in a write-locked transaction; submit needs an accepted AFTER photo; reject/cancel need a reason; accept completes the plan through workflow verification | activity, notification |
| GET | `/api/work-orders/{id}/evidence/{eid}/photo` | O · A · linked C (AFTER photos of VERIFIED work only) | private store | — |
| POST | `/api/admin/users/{id}` | A | role/department/active; role or access change revokes tokens; cannot demote yourself | activity |
| GET/POST | `/api/admin/departments[/{id}/state]` | A | routing departments from config cannot be deactivated | activity |
| GET | `/api/admin/analytics` | A | computed from records at request time | — |
| POST | `/api/utility/connections`, `/api/utility/contacts`, `/api/utility/assets/{id}/status` | A | stored unverified; same-network connections only; duplicates refused | activity |
| POST | `/api/utility/import/rollback` | A | typed `ROLLBACK`; refused if anything depends on the imported records | activity |

## TinyFish (public data-source discovery)

- `TINYFISH_API_KEY` is read on the server only and sent only in the `X-API-Key` header to `https://*.tinyfish.ai`
  (other hosts or plain http are refused). Provider redirects are not followed, so the header cannot leak.
  The key never appears in responses, logs (host + status only), errors or the frontend.
- **URL safety layer** (`services/tinyfish/url_safety.py`): only http/https; no embedded credentials; no
  localhost, single-label, `.local`/`.internal`/… names; no private, loopback, link-local, CGNAT, reserved,
  multicast or IPv4-mapped addresses; no numeric host spellings (`2130706433`, `0x7f.1`, `127.1`); host names
  that resolve to a non-public address are refused. Redirect targets returned by Fetch are re-checked and
  their content discarded if unsafe; a change of site is flagged.
- Search results are *candidates*; fetched text is *untrusted evidence*. Neither writes infrastructure records.
  Mock content (`TINYFISH_MOCK_MODE`) is labelled everywhere and gets provenance `synthetic_demo`.
- Retries: Search only (GET); Fetch only when the service did not process the batch (429/503); Agent never.

## Multilingual intake

- `complaints.description` is always the citizen's original text. Translations are stored separately in
  `description_translation_en`, only when the citizen ticked that they reviewed it, labelled `citizen_reviewed`.
- No translation is produced without a configured AI provider; the API reports `translation_status: unavailable`.
- Place names returned by the assistant are hints; the location is always the citizen's map pin.

## Other controls

- **Request limits**: 9 MB per request; 5 MB / 40 MP per image; text fields have max lengths.
- **Headers**: `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, CSP
  (`script-src` still allows `'unsafe-inline'` because pages use inline scripts; external scripts are limited
  to unpkg / jsdelivr), `Cache-Control: no-store` on API responses.
- **Errors**: unhandled errors return a generic message with a reference id; the traceback goes to the server
  log only. Validation errors list field names, never the submitted values.
- **CORS**: disabled (same-origin) unless `CIVICMIND_CORS_ORIGINS` is set.
- **AI**: model output is schema-validated and clamped; routing, priority, evidence verdicts, state transitions
  and authorisation are deterministic code. Citizen text is sent as JSON data with an explicit
  "untrusted data" instruction.
- **Concurrency**: state changes run in `BEGIN IMMEDIATE` transactions and update with `WHERE status = <expected>`;
  a lost race returns 409 and changes nothing. Public ids derive from AUTOINCREMENT keys (never reused).

## Deployment

- `CIVICMIND_ENV=production` refuses to start without a fixed 32+ character `CIVICMIND_SECRET`, with demo mode,
  on the demo database or with TinyFish mock mode.
- `Strict-Transport-Security` is sent on HTTPS requests (directly or via a proxy forwarding `X-Forwarded-Proto`).
- No page or endpoint lists accounts or credentials, in any mode.

## Known gaps

- Login throttling is per process (not shared across workers/instances).
- Tokens live in `localStorage`; a successful XSS could read them. Rendering escapes all untrusted text,
  but CSP still allows inline scripts.
- SSE passes the token in the query string (EventSource cannot send headers); it is redacted from uvicorn
  access logs but may appear in proxy logs — configure the proxy accordingly.
- No department/ward-level officer scoping; any officer can act on any incident.
