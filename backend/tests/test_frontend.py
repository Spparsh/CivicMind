"""Frontend checks that run without a browser.

1. Every API path referenced by the JavaScript exists on the backend.
2. XSS regression: the incident/dashboard renderers are executed in Node with
   hostile strings; the output must contain them only in escaped form.
These are NOT a substitute for end-to-end browser testing.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

import config
import main

JS_DIR = os.path.join(config.FRONTEND_DIR, "js")


def _route_patterns():
    pats = []
    for r in main.app.routes:
        path = getattr(r, "path", None)
        if path and path.startswith("/api/"):
            pats.append(re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$"))
    return pats


def test_every_frontend_api_call_has_a_backend_route():
    pats = _route_patterns()
    missing = []
    for name in os.listdir(JS_DIR):
        src = open(os.path.join(JS_DIR, name), encoding="utf-8").read()
        for m in re.finditer(r"""['"`](/?api/[^'"`\s]*)""", src):
            raw = m.group(1)
            path = "/" + raw.lstrip("/")
            path = re.sub(r"\$\{[^}]*\}", "X", path).split("?")[0]
            if path.endswith("/"):
                path += "X"  # concatenated id, e.g. '/api/complaints/' + id
            if not any(p.match(path) for p in pats):
                missing.append(f"{name}: {raw}")
    assert not missing, missing


NODE = shutil.which("node")
EVIL = '</textarea><img src=x onerror=alert(1)>"\'<script>alert(2)</script>'

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const ctx = { console, setTimeout, setInterval, clearInterval, URLSearchParams, encodeURIComponent,
  location: { protocol: 'http:', search: '?id=1', pathname: '/incident.html', href: '' },
  localStorage: { getItem: (k) => k === 'cm_user' ? JSON.stringify({ role: 'officer', name: 'O' }) : 't', setItem(){}, removeItem(){} },
  document: { addEventListener(){}, getElementById(){ return null; }, querySelectorAll(){ return []; } },
  navigator: {}, L: undefined };
vm.createContext(ctx);
for (const f of ['api.js', 'common.js', 'incident.js']) vm.runInContext(fs.readFileSync(process.argv[2] + '/' + f, 'utf8'), ctx);
const data = JSON.parse(process.argv[3]);
vm.runInContext('inc = ' + JSON.stringify(data), ctx);
process.stdout.write(vm.runInContext('staffView(inc) + citizenView(' + JSON.stringify(data.citizen) + ')', ctx));
"""


UTIL_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const ctx = { console, setTimeout, setInterval, clearInterval, URLSearchParams, encodeURIComponent,
  location: { protocol: 'http:', search: '?id=1', pathname: '/incident.html', href: '' },
  localStorage: { getItem: (k) => k === 'cm_user' ? JSON.stringify({ role: 'officer', name: 'O' }) : 't', setItem(){}, removeItem(){} },
  document: { addEventListener(){}, getElementById(){ return null; }, querySelectorAll(){ return []; } },
  navigator: {}, L: undefined };
vm.createContext(ctx);
for (const f of ['api.js', 'common.js', 'utility-network.js', 'incident.js']) vm.runInContext(fs.readFileSync(process.argv[2] + '/' + f, 'utf8'), ctx);
const d = JSON.parse(process.argv[3]);
vm.runInContext('D = ' + JSON.stringify(d), ctx);
process.stdout.write(vm.runInContext(`UtilityLayer.renderDetail(D.detail) + UtilityLayer.renderTrace(D.trace, 'trace')
  + UtilityLayer.renderTrace(D.iso, 'isolation') + UtilityLayer.renderAnswer(D.answer) + UtilityLayer.renderHistory(D.history)
  + utilityContextCard(D.ctx, 1) + briefingView(D.briefing) + workOrdersCard(D.wos, D.cons, D.winc)`, ctx));
"""

CITIZEN_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const ctx = { console, setTimeout, setInterval, clearInterval, URLSearchParams, encodeURIComponent,
  location: { protocol: 'http:', search: '', pathname: '/citizen.html', href: '' },
  localStorage: { getItem: (k) => k === 'cm_user' ? JSON.stringify({ role: 'citizen', name: 'C' }) : 't', setItem(){}, removeItem(){} },
  document: { addEventListener(){}, getElementById(){ return null; }, querySelectorAll(){ return []; }, documentElement: {} },
  navigator: {}, L: undefined };
vm.createContext(ctx);
for (const f of ['api.js', 'common.js', 'i18n.js', 'voice-recorder.js', 'citizen.js']) vm.runInContext(fs.readFileSync(process.argv[2] + '/' + f, 'utf8'), ctx);
vm.runInContext('C = ' + process.argv[3], ctx);
process.stdout.write(vm.runInContext('complaintCard(C)', ctx));
"""


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_citizen_card_escapes_untrusted_text(tmp_path):
    c = {"id": 1, "public_id": EVIL, "category": EVIL, "ward": EVIL, "status": "in_progress", "description": EVIL,
         "has_photo": True, "next_step": EVIL, "tracking_code": EVIL, "created_at": "2026-01-01 00:00:00",
         "updated_at": "2026-01-01 00:00:00", "incident": {"id": 1, "public_id": EVIL},
         "language": {"translation_en": EVIL}, "estimate": {"kind": EVIL, "at": None, "basis": EVIL},
         "voice": {"transcript": EVIL, "transcription_status": EVIL},
         "resolution_photos": [{"url": EVIL, "label": EVIL}],
         "media": [{"kind": "video", "url": EVIL}, {"kind": EVIL, "url": EVIL, "is_main_photo": False}],
         "history": [{"label": EVIL, "at": "2026-01-01 00:00:00"}]}
    script = tmp_path / "c.js"
    script.write_text(CITIZEN_HARNESS, encoding="utf-8")
    out = subprocess.run([NODE, str(script), JS_DIR, json.dumps(c)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    for raw in ("<img src=x", "<script>", "</textarea><img", "\" onerror"):
        assert raw not in out.stdout, raw
    assert "&lt;/textarea&gt;&lt;img src=x onerror=alert(1)&gt;" in out.stdout


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_utility_renderers_escape_untrusted_text(tmp_path):
    a = {"asset_id": EVIL, "name": EVIL, "utility_type": EVIL, "asset_type": EVIL, "verification_status": EVIL,
         "provenance_class": EVIL, "is_synthetic": True, "lat": 18.5, "lng": 73.8, "path": [EVIL], "side": EVIL,
         "path_verified": False, "unverified_records": [EVIL], "hop": 1, "via": EVIL}
    edge = {"connection_id": EVIL, "from": EVIL, "to": EVIL, "direction": "unknown", "verification_status": EVIL}
    data = {
        "detail": a | {"utility_label": EVIL, "criticality": EVIL, "notes": [EVIL], "operational_status": EVIL,
                       "operator_org": EVIL, "geometry_type": EVIL, "length_m": EVIL, "attributes": {EVIL: EVIL},
                       "data_source": {"name": EVIL}, "last_verified_at": None, "isolation_applicable": True,
                       "connections": [{"relation": EVIL, "asset": a, "connection": edge}],
                       "contacts": [{"organization": EVIL, "role": EVIL, "phone": EVIL, "label": EVIL, "source": EVIL,
                                     "operational_use": False, "is_synthetic": True, "last_verified_at": None}]},
        "trace": {"direction": "upstream", "visited_assets": [a], "edges": [edge], "possible_sources": [a],
                  "candidate_isolation_assets": [a], "warnings": [EVIL], "unknown_links": [edge], "dead_ends": [EVIL],
                  "note": EVIL},
        "iso": {"upstream_candidates": [a], "downstream_candidates": [a], "sources": [a], "warnings": [EVIL],
                "unknown_links": [edge], "note": EVIL},
        "answer": {"answer": EVIL, "data_gaps": [EVIL], "engine": "records", "citations": ["WM-" + EVIL]},
        "history": {"incidents": [{"id": 1, "public_id": EVIL, "title": EVIL, "status": EVIL, "link_status": EVIL,
                                   "complaints": [{}]}],
                    "maintenance": [{"work_type": EVIL, "outcome": EVIL, "performed_on": "2026-01-01", "record_id": EVIL}]},
        "ctx": {"note": EVIL, "data_gaps": [EVIL], "rejected": [a],
                "candidates": [a | {"distance_m": EVIL, "reason": EVIL, "link_status": EVIL}]},
        "briefing": {"engine": "records", "summary": EVIL, "facts": [{"text": EVIL, "refs": [EVIL]}], "hypotheses": [EVIL],
                     "data_gaps": [EVIL], "note": EVIL,
                     "checklist": {"steps": [{"step": EVIL, "detail": EVIL, "owner": EVIL}]}},
        "wos": [{"id": 1, "public_id": EVIL, "task": EVIL, "status": s, "department": EVIL, "priority": EVIL,
                 "overdue": True, "team_name": EVIL, "est_start": None, "est_completion": None, "actual_start": None,
                 "actual_completion": None, "verification_note": EVIL,
                 "contractor": {"name": EVIL, "contractor_code": EVIL, "contact_person": EVIL, "contact_role": EVIL,
                                "phone": EVIL, "office_address": EVIL, "label": EVIL, "is_synthetic": True,
                                "verification_status": EVIL},
                 "evidence": [{"id": 2, "kind": EVIL, "check": {"verdict": EVIL}}],
                 "history": [{"created_at": "2026-01-01 00:00:00", "actor": EVIL, "kind": EVIL, "to_status": EVIL,
                              "new_value": EVIL, "note": EVIL}]}
                for s in ("assigned", "in_progress", "awaiting_verification")],
        "cons": [{"id": 3, "name": EVIL, "is_active": True, "is_synthetic": True}],
        "winc": {"id": 1, "current_action": {"status": "approved", "action_text": EVIL, "assigned_team": EVIL}},
    }
    script = tmp_path / "u.js"
    script.write_text(UTIL_HARNESS, encoding="utf-8")
    out = subprocess.run([NODE, str(script), JS_DIR, json.dumps(data)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    html = out.stdout
    assert len(html) > 2000
    for raw in ("<img src=x", "<script>", "</textarea><img", "\" onerror"):
        assert raw not in html, raw
    assert "&lt;/textarea&gt;&lt;img src=x onerror=alert(1)&gt;" in html


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_incident_renderer_escapes_untrusted_text(tmp_path):
    action = {"id": 1, "status": "pending", "action_text": EVIL, "assigned_team": EVIL, "department": EVIL,
              "eta_hours": 24, "checklist": [EVIL, "ok"], "source": "rules", "has_proof": False,
              "rejection_reason": EVIL}
    inc = {
        "view": "staff", "id": 1, "public_id": "INC-1", "title": EVIL, "category": EVIL, "status": "active",
        "ward": EVIL, "created_at": "2026-01-01 00:00:00", "report_count": 1, "distinct_reporters": 1,
        "spread_km": 0, "priority": {"label": "High", "score": 61, "factors": [
            {"label": EVIL, "points": 10, "max": 35, "detail": EVIL}], "reasons": [EVIL], "model": EVIL},
        "investigation": {"summary": EVIL, "why_linked": [EVIL], "engine": "rules"},
        "complaints": [{"id": 1, "public_id": EVIL, "category": EVIL, "description": EVIL, "status": "clustered",
                        "severity": 3, "latitude": 18.5, "longitude": 73.8, "created_at": "2026-01-01 00:00:00",
                        "citizen_name": EVIL, "has_photo": False, "asset_match": None, "is_synthetic": False,
                        "language": {"original": "mr", "translation_en": EVIL},
                        "media": [{"kind": "video", "url": EVIL}, {"kind": "photo", "url": EVIL}]}],
        "coordination": {"isolation_status": "awaiting_operator", "safety_note": EVIL,
                         "steps": [{"key": EVIL, "label": EVIL}],
                         "updates": [{"step_label": EVIL, "operator_org": EVIL, "reference": EVIL,
                                      "contact_ref": EVIL, "asset_ref": EVIL, "note": EVIL,
                                      "recorded_by_name": EVIL, "created_at": "2026-01-01 00:00:00"}]},
        "actions": [action, {**action, "id": 0, "status": "rejected"}], "current_action": action,
        "can_generate_plan": False, "resolution": None,
        "asset": {"id": 1, "asset_code": EVIL, "criticality": "critical", "type_label": EVIL, "name": EVIL,
                  "criticality_reason": EVIL, "matched_reports": 1, "min_distance_m": 5, "is_synthetic": 1,
                  "recent_maintenance": [{"event_type": "repair", "performed_at": "2026-01-01", "outcome": EVIL,
                                          "data_source": EVIL, "is_synthetic": 1}]},
        "findings": [{"id": 1, "severity": "high", "rule_title": EVIL, "explanation": EVIL,
                      "recommended_step": EVIL, "status": "open", "is_synthetic": 0}],
        "timeline": [{"actor": EVIL, "detail": EVIL, "created_at": "2026-01-01 00:00:00"}],
        "grouping_basis": {"radius_km": 1.5, "window_hours": 48, "min_reports": 3, "categories": [EVIL]},
        "citizen": {"view": "citizen", "public_id": EVIL, "title": EVIL, "status": "active", "report_count": 2,
                    "priority": {"label": "High"}, "current_action": {"status": "approved", "action_text": EVIL,
                    "department": EVIL}, "resolution": None, "timeline": [{"action": EVIL, "created_at": ""}],
                    "my_reports": [{"public_id": EVIL, "status": "clustered"}]},
    }
    script = tmp_path / "h.js"
    script.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run([NODE, str(script), JS_DIR, json.dumps(inc)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    html = out.stdout
    assert len(html) > 1000
    for raw in ("<img src=x", "<script>", "</textarea><img", "\" onerror"):
        assert raw not in html, raw
    assert "&lt;/textarea&gt;&lt;img src=x onerror=alert(1)&gt;" in html
    # the edit-form textareas specifically
    assert re.search(r'<textarea id="eCheck">&lt;/textarea&gt;', html)
