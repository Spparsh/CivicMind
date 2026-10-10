# CivicMind — live demo script (≈ 7 minutes)

Everything shown is **fictional** demo data in an isolated database, tagged *Synthetic* in the UI.
Nothing is changed behind the scenes between steps — every state change below is made in the app.

## Before you present

1. `run_demo.bat --reset` (or `./run_demo.sh --reset`) to start from a clean, known state.
2. Open `data/demo/demo_credentials.txt` for the logins.
3. Keep `data/demo/sample_proof_photo.jpg` handy for the proof upload.
4. Optional: unset `SARVAM_API_KEY` (or set `CIVICMIND_DISABLE_AI=1`) for fully deterministic output and no
   network dependency. With a key, classification and plan text may differ slightly; the workflow does not.

## What the seed contains

| Item | Detail |
|---|---|
| Assets | `SL-DEMO-014` critical streetlight at a school crossing (repaired 20 days ago); `SL-DEMO-015` streetlight ~200 m away; `RD-DEMO-007` road segment last inspected 400 days ago (interval 180); a drain and a waste point |
| Reports | 3 streetlight reports by 3 citizens within ~30 m over 30 h; 1 pothole near the bus stop; 1 garbage report |
| Result | Incident `INC-00001` (3 reports) with a pending plan; findings A, B, C, E |

## Script

**1. The problem (landing page, 30 s).** Complaints arrive one at a time; the underlying fault — one
streetlight at a school crossing — is invisible in a list of tickets.

**2. Citizen view (1 min)** — sign in as `citizen@civicmind.in`.
- Show *My reports*: tracking code, progress steps, "what happens next".
- Optionally file a new report (any category, pin the map). Point out the private tracking code and the
  classification engine label. *Don't* file another streetlight report next to the school before step 5
  unless you want to show it joining the incident.
- Track a report with its code in an incognito window: status only, no personal data.

**3. Operations workspace (1.5 min)** — sign in as `officer@civicmind.in`.
- Metrics, each with a hover definition. Priority queue: *why* `INC-00001` ranks — critical asset linked,
  3 matching reports.
- Risk findings panel:
  - **A** 3 open reports matched to `SL-DEMO-014`.
  - **B** reports filed ~18 days after the lamp was replaced — a *possible* recurrence, not proof of failure.
  - **C** open incident on a critical asset.
  - **E** `RD-DEMO-007` inspection overdue by ~220 days.

**4. Infrastructure intelligence (1 min)** — *Infrastructure* page or map.
- `SL-DEMO-014`: maintenance history (fictional), linked reports with distances, findings.
- Note: `SL-DEMO-015` is also a streetlight but ~200 m away — outside the 150 m match radius, so it was not
  linked. A nearby road asset is never matched to a streetlight report.
- Map: toggle reports / incidents / assets / findings layers.

**5. Incident workspace and resolution (2 min)** — open `INC-00001`.
- Priority breakdown bars, grouping rule, linked asset, investigation notes (engine label).
- Plan is a *recommendation*: edit it or approve it. (Optional: reject with a reason, then *Draft a new plan* —
  only one open plan is allowed.)
- *Mark work started* → citizens' reports move to "In progress".
- First, show the guard: try `data/demo/blank_test_photo.png` (or a photo already used in a report) as proof → rejected
  with reasons, nothing changes.
- Upload `sample_proof_photo.jpg` with notes → *Submit proof & complete*.
  - Incident becomes **Resolved**; verification card shows "0 new related reports since work" and states it is
    not an independent field inspection.
  - A repair is logged on `SL-DEMO-014` automatically; findings A and C clear immediately (completion re-runs
    the rules), B stays open for review (the recurrence pattern is historical fact) and can be acknowledged.

**6. Accountability loop (30 s)** — back as the citizen: report shows *Resolved by officer Demo Officer*, and a
notification arrived. Admin (`admin@civicmind.in`): complaint and resolution oversight with proof photos.

**7. Close (30 s).** CivicMind connects individual complaints to the asset behind them, explains every priority
and finding, and only closes work with proof — with privacy enforced on the server.

## Honest answers to likely questions

- *Is the AI deciding things?* No. It suggests category/severity and drafts text. Grouping, matching, priority,
  findings, photo checks and every state change are deterministic code.
- *Is the photo check proof the repair happened?* No — it checks the file is a valid, non-blank, never-before-used
  photo. Field verification would need an inspector or sensors.
- *Real ward boundaries?* Not yet — nearest ward centre, labelled as approximate.
- *Real municipal data?* No. The asset model and GeoJSON import are ready for it; the demo uses fictional data.
