# CivicMind — integration and verification report

Status date: 2026-10-10 · Branch `claude-platform` (from `master` @ `7abba6d`) · local commits only, not pushed.

Legend: **PASS** tested and verified · **PARTIAL** implemented, incompletely verified · **FAIL** tested, not
working · **BLOCKED** could not be verified (reason given) · **NOT IMPLEMENTED**.

## Verification summary

| Area | Status | Evidence |
|---|---|---|
| Existing app preserved | PASS | All 84 original tests still pass; additive migrations only; demo DB upgraded in place without data loss. |
| Light-theme design system + landing page | PASS (visual check) | New tokens applied to every page; landing rebuilt in place using the real sign-in/registration; checked in the browser. No automated visual test. |
| Three portals + server-role routing | PASS | `/citizen` `/officer` `/admin` redirects, server-chosen home, page role re-check; tests in `test_coordination_portals.py`; browser check. |
| Citizen multilingual intake (EN/HI/MR) | PASS (rule-based) · PARTIAL (AI) | Labels, statuses and next steps localised; Hindi/Marathi rule classification; original text preserved; translation only via AI and only when citizen-reviewed. Sarvam path tested with a mocked transport only. |
| Civic help chatbot | PASS (knowledge base) · PARTIAL (AI phrasing) | Tests: own-complaints only, draft never submits, emergency → 112, AI text adding numbers/links rejected (mocked). Browser: Marathi answer and leak draft. Live AI rephrasing not characterised. |
| Voice description (15 s) + STT | PASS (validation, storage, privacy) · PARTIAL (STT) | Tests: WAV format/duration limits, single-use link, owner/staff-only playback, purge, mocked Sarvam response. One **live Sarvam STT call** made with a 2 s test tone → `status: empty` (endpoint, auth and format accepted). Transcription of real speech and its accuracy **not verified**. |
| 1–3 photos or videos per report | PASS | 10 tests (MP4/MOV/WebM parsing, duration limit, no-video-track, damaged files, location tag blanked, size caps, 3-file limit, single-use tokens, ownership, private playback, purge, re-use as proof refused). Browser: 2 photos + a real browser-recorded WebM uploaded, reviewed, submitted (CMP-00011), shown and played on the report card. Video length is only enforced when the file declares it. |
| "Use my current location" + address autofill | PASS (live) | Live Nominatim reverse lookup verified on 2026-10-10 from the demo (simulated device position in the preview pane; real phone GPS not tested). Enabled in the demo launchers; off by default elsewhere. |
| Address search → pin + address | PASS (live) | Live Nominatim search ("Fergusson College Road") returned a Pune result; choosing it moved the pin and filled the address. Tests: bounded/viewbox request, out-of-area results dropped, cache, throttle, offline ward-name fallback. |
| Review step, tracking ID, status history, estimate, resolution photos | PASS | Tests for history labels (no internal notes to citizens), estimate kinds actual/scheduled/estimated/pending; browser check of review → edit (nothing submitted) and report cards. |
| Work orders + contractors | PASS | Lifecycle test (create → start → progress → after-photo → submit → reject → resubmit → accept → incident verified), invalid transitions 409, citizen access limits, cancel, overdue metric; browser check of WO-00001. |
| Officer dashboard metrics (overdue, awaiting verification) | PASS | Computed from records; browser check. |
| Admin users/departments/contractors/analytics | PASS | Tests for role change + token revocation, self-demotion refused, config departments locked; browser check. |
| Registry editing (connections, contacts, status) + import rollback | PASS | Tests: unverified on create, cross-utility/duplicate refused, rollback blocked by dependants, typed confirmation. |
| Department-scoped officer access | NOT IMPLEMENTED | Departments are recorded on users, not enforced. |
| Text-similarity complaint grouping | NOT IMPLEMENTED | The existing explainable grouping (category + distance + time) is kept. |
| Street-following synthetic network (~6,100 records, 6 utilities) | PASS | Generated along OpenStreetMap street centre-lines (bundled extract, ODbL); test checks a sample of lines stays within 12 m of a street centre-line, every utility has > 300 records and no record is unconnected; core leak-scenario tests unchanged and passing. Browser check: parallel colour-coded utilities along Kothrud streets at zoom 17–18. |
| Operations dashboard layout, collapsible map legend | PASS (visual check) | Live operations mini-map, open work orders, network summary and charts fill the former blank column; legend collapses and sits beside the filter panel (no overlap measured at 1280×1000). |
| Utility registry, geometry, provenance | PASS | `test_utility_network.py`; asset panel verified in the browser on the demo DB. |
| Deterministic tracing (up/down/both, limits, loops, gaps) | PASS | Tests for upstream source, nearest valve per branch, unknown-direction link not walked, loop detection, missing link not invented, hop limit, per-utility semantics. |
| Isolation candidates | PASS | Candidates only, with path and verification state; admin verification changes the state; browser check. |
| Incident ↔ asset candidates, confirm/reject | PASS | Test + browser (WM-DEMO-03 at ≈3 m ranked first with reason). |
| Utility findings U1–U6 | PASS (U1–U3, U5 exercised) · PARTIAL (U4, U6 rules implemented, not individually tested) | Findings name rule and triggering records; one focus asset per incident to avoid noise. |
| Utility Copilot / incident briefing / checklist | PASS (record-based) · PARTIAL (AI phrasing) | Tests prove fabricated IDs from AI are rejected and grounded text is accepted (mocked). Live Sarvam call during the browser check fell back to the record answer; why (timeout vs. rejection) was not investigated. |
| Operator coordination (water-leak workflow) | PASS | Full sequence test incl. prerequisites, proof photo, verification; browser check of recording and refusal. |
| GeoJSON/CSV import preview → confirm | PASS | Tests (duplicates, swapped coordinates, CRS, missing relationships, idempotent confirm, unverified on import); browser preview check (not confirmed, to keep the demo DB clean). |
| Shapefile / KML / PDF extraction | NOT IMPLEMENTED | — |
| TinyFish Search/Fetch/Agent adapter | PASS (mocked) · live BLOCKED | 65 tests against documented request/response shapes; no `TINYFISH_API_KEY` in this environment, so no live call was made. |
| TinyFish → reviewed source → import provenance | PASS | Unreviewed snapshot refused (409); mock content classed `synthetic_demo`; browser "Use as the source of a utility import" link. |
| Admin: users, utility overview, findings, import history, audit log | PASS | Tests for audit access; browser check of all sections. |
| Admin: editable categories/thresholds | NOT IMPLEMENTED | Configuration remains read-only (`data/civicmind_config.json`). |
| Security: authorisation, privacy, XSS, uploads, transitions | PASS | Role tests on every new route; citizens get 403 on all utility data; Node XSS harness over all new renderers; existing upload/transition tests unchanged and passing. |
| Map tiles | PASS | Referrer policy fixed so OpenStreetMap serves tiles; regression test. |

## Test run (executed)

`cd backend && python -m pytest` → **216 passed, 0 skipped** (Windows, Python 3.14.3, Node 25.8.1).

| File | Tests |
|---|---|
| test_tinyfish.py | 65 |
| test_media_and_search.py | 10 |
| test_ai_and_integrity.py | 18 |
| test_auth_permissions.py | 22 |
| test_utility_network.py | 18 |
| test_platform_features.py | 15 |
| test_workflow.py | 14 |
| test_infrastructure.py | 13 |
| test_multilingual.py | 12 |
| test_evidence.py | 9 |
| test_privacy.py | 8 |
| test_coordination_portals.py | 8 |
| test_frontend.py (API-path coverage + 3 Node XSS harnesses) | 4 |

## Manual browser verification (demo database, Sarvam on, TinyFish mock)

- Landing page renders with live counts; sign-in routes admin/officer/citizen to their portals.
- Officer map: utility layers drawn from `/api/utility/assets` (dashed = unverified); `?asset=WM-DEMO-03` opens the
  asset panel; *Find isolation candidates* → V-DEMO-02 (upstream) and V-DEMO-04 (downstream), loop link UC-DEMO-13
  flagged; Copilot answered "Where does the water come from?" from records with citations.
- Incident INC-00002 (fictional leak): candidates ranked by distance; WM-DEMO-03 confirmed; briefing shows facts
  with record IDs, a labelled hypothesis, data gaps and the checklist (fictional contact labelled as such).
- Admin: utility overview (32 assets, 27 connections), findings, audit log; import preview reported a duplicate and
  a missing relationship and saved nothing.
- Earlier session: citizen Marathi report stored exactly as typed; TinyFish mock discover → fetch → approve.
- This phase: officer incident shows work order WO-00001 (fictional contractor, scheduled completion); citizen
  (Marathi) chatbot answer and leak draft; voice recorder uploaded a test tone (live Sarvam → "no speech");
  review panel → Edit returned to the form without submitting; report cards show history and "pending"
  estimate; admin departments, contractors, users and analytics render. Server log had no errors.

## Remaining issues

1. **Live TinyFish not verified** — add `TINYFISH_API_KEY` to `.env`; follow the README live check.
2. **Live Sarvam behaviour** of the Copilot/briefing phrasing was not characterised; the record-based fallback works.
   The Copilot can wait up to the Sarvam timeout (25 s) before answering.
3. Shapefile/KML/PDF import, editable configuration and department-scoped officer access are not implemented.
   Speech-to-text accuracy on real speech is unverified; location autofill was verified live with a simulated device position, not on a phone.
3a. The demo DB seeded before photos became mandatory keeps its older photo-less reports; the synthetic Marathi
   voice transcript appears only after `run_demo.bat --reset`. Estimate basis text is English-only.
4. Legacy `POST /api/assets/import` (point registry) still saves without a preview; it is labelled as such in the UI.
5. Hindi/Marathi strings need native-speaker review.
6. Single-instance assumptions (in-process throttles/caches), SQLite only.
7. If an Astra utility branch is still delivered, reconcile it with this implementation instead of merging both.

## Demo runbook

See the README "Demo walkthrough". Start with `run_demo.bat`; logins are in `data/demo/demo_credentials.txt`.
`run_demo.bat --reset` restores the demo civic data (users, legacy assets and the synthetic network are kept).
