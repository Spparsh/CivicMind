"""
CivicMind — Utility Copilot, incident briefing and coordination checklist.

Grounding rule: every answer is first assembled DETERMINISTICALLY from the
registry, the trace engine and incident records, as a list of facts with the
record IDs that support them. An AI provider (if configured) may only
rephrase those facts; its text is rejected if it mentions any asset or record
ID that is not among the cited records, and the record-based answer is used
instead. The copilot never writes data and never authorizes an operation.
"""
import json
import re

from database import query
from services import ai, coordination
from services.utility_network import findings, graph, matching, model, registry

_ID_TOKEN = re.compile(r"\b[A-Z]{1,6}(?:-[A-Z0-9]{1,10}){1,4}\b")
INTENTS = [
    ("isolation", ("isolat", "valve", "shut", "switch", "breaker", "control", "close")),
    ("source", ("upstream", "source", "feed", "supplied", "supply", "where does the water come", "origin")),
    ("downstream", ("downstream", "branch", "serve", "affected", "beyond")),
    ("operator", ("operator", "contact", "who ", "phone", "call", "responsible")),
    ("complaints", ("complaint", "report", "incident")),
    ("maintenance", ("maintenance", "repair", "inspect", "work ", "history")),
    ("unverified", ("unverified", "verify", "verified", "confidence", "provenance", "reliable", "missing", "gap")),
    ("connections", ("connect", "attached", "linked", "neighbour", "neighbor", "join")),
]
SAFETY = ("Coordination aid only — CivicMind does not operate valves, switches or gas controls. The authorized "
          "operator identifies and operates the actual control point under approved procedures.")


def _intent(q):
    ql = (q or "").lower()
    for name, words in INTENTS:
        if any(w in ql for w in words):
            return name
    return "summary"


def _fact(text, *refs):
    return {"text": text, "refs": [r for r in refs if r]}


def _asset_facts(a, intent):
    code = a["asset_id"]
    facts, gaps = [], []
    if intent in ("connections", "summary"):
        ns = registry.neighbours(a)
        if not ns:
            gaps.append(f"No connections are recorded for {code}.")
        for n in ns:
            c = n["connection"]
            facts.append(_fact(f"{code} has a recorded {n['relation']} connection to {n['asset']['asset_id']} "
                               f"({n['asset']['name']}) via {c['connection_id']} — {c['verification_status']}.",
                               n["asset"]["asset_id"], c["connection_id"]))
    if intent in ("source", "summary"):
        up = graph.trace(code, "upstream")
        for s in up["possible_sources"]:
            facts.append(_fact(f"Recorded upstream source: {s['asset_id']} ({s['name']}), path "
                               f"{' → '.join(s['path'])}.", s["asset_id"], *s["path"]))
        gaps += [w for w in up["warnings"] if "source" in w or "unknown" in w]
        for d in up["dead_ends"]:
            gaps.append(f"The upstream record stops at {d}: no further recorded connection.")
    if intent == "downstream":
        down = graph.trace(code, "downstream")
        branch = [v for v in down["visited_assets"] if v["hop"] > 0]
        if not branch:
            gaps.append(f"No downstream connections are recorded from {code}.")
        for v in branch:
            facts.append(_fact(f"Downstream (hop {v['hop']}): {v['asset_id']} — {v['name']}.", v["asset_id"], v["via"]))
        gaps += [w for w in down["warnings"] if "unknown" in w or "loop" in w]
    if intent in ("isolation", "summary"):
        iso = graph.isolation(code)
        if not iso["applicable"]:
            gaps.append(f"No isolation devices are modelled for {model.UTILITY_TYPES[a['utility_type']]['label']}.")
        for side, items in (("upstream", iso["upstream_candidates"]), ("downstream", iso["downstream_candidates"])):
            for c in items:
                facts.append(_fact(f"Candidate {side} control device: {c['asset_id']} ({c['name']}), path "
                                   f"{' → '.join(c['path'])}; "
                                   + ("all records on the path are verified." if c["path_verified"] else
                                      f"unverified records on the path: {', '.join(c['unverified_records'])}."),
                                   c["asset_id"], *c["path"]))
        if iso["applicable"] and not iso["upstream_candidates"]:
            gaps.append("No recorded upstream control device was found within the trace limit.")
        if intent == "isolation":
            gaps += [w for w in iso["warnings"] if "unknown" in w or "loop" in w or "source" in w]
    if intent in ("operator", "summary"):
        cs = registry.contacts_for(a)
        usable = [c for c in cs if c["operational_use"]]
        for c in cs:
            facts.append(_fact(f"{c['organization']} — {c['role']}: {c['phone'] or c['email'] or 'no channel recorded'} "
                               f"[{c['label']}; last verified {c['last_verified_at'] or 'never'}].", c["contact_id"]))
        if not usable:
            gaps.append("No verified operator contact is recorded for this asset.")
    if intent in ("complaints", "maintenance", "summary"):
        h = registry.history(a)
        if intent in ("complaints", "summary"):
            for i in h["incidents"]:
                facts.append(_fact(f"Incident {i['public_id']} ({i['status']}, link {i['link_status']}) with "
                                   f"{len(i['complaints'])} report(s): {', '.join(c['public_id'] for c in i['complaints'])}.",
                                   i["public_id"], *[c["public_id"] for c in i["complaints"]]))
            if not h["incidents"]:
                gaps.append(f"No incidents are linked to {code}.")
        if intent in ("maintenance", "summary"):
            for m in h["maintenance"]:
                facts.append(_fact(f"{m['work_type'].title()} on {m['performed_on'][:10]}: {m['outcome'] or 'no outcome recorded'} "
                                   f"({m['record_id']}).", m["record_id"]))
            if not h["maintenance"]:
                gaps.append(f"No maintenance records exist for {code}.")
    if intent in ("unverified", "summary"):
        t = graph.trace(code, "both")
        if a["verification_status"] != "verified":
            facts.append(_fact(f"{code} itself is {a['verification_status']} ({model.PROVENANCE_LABELS.get(a['provenance_class'])}).", code))
        for cid in t["unverified_connections"]:
            facts.append(_fact(f"Connection {cid} is not field-verified.", cid))
        for u in t["unknown_links"]:
            facts.append(_fact(f"Connection {u['connection_id']} ({u['from']} – {u['to']}) has unknown flow direction.",
                               u["connection_id"]))
        if intent == "unverified" and a["verification_status"] == "verified" and not t["unverified_connections"]:
            facts.append(_fact(f"{code} and the connections traced around it are recorded as verified.", code))
    if a["is_synthetic"]:
        facts.append(_fact("These are fictional synthetic demo records, not real infrastructure.", code))
    return facts, list(dict.fromkeys(gaps))


def _grounded_ai(question, facts, allowed):
    """Optional rephrasing. Returns text only if it cites nothing outside `allowed`."""
    if not ai.ai_available() or not facts:
        return None
    system = ("You are CivicMind's Utility Copilot for municipal officers. " + ai._INJECTION_GUARD +
              " Answer the officer's question in at most 120 words using ONLY the supplied facts. Cite record IDs "
              "exactly as written. If the facts do not answer the question, say what is missing. Never invent assets, "
              "connections, contacts or procedures, and never tell anyone to operate equipment. "
              'Respond with STRICT JSON: {"answer": string}.')
    try:
        out = ai._extract_json(ai._chat([{"role": "system", "content": system},
                                         {"role": "user", "content": json.dumps(
                                             {"question": question[:300], "facts": [f["text"] for f in facts]})}],
                                        temperature=0.1, max_tokens=800))
        text = ai._s((out or {}).get("answer"), 900)
        if not text or any(tok not in allowed for tok in _ID_TOKEN.findall(text)):
            return None
        return text
    except Exception:  # noqa: BLE001 — fall back to the record-based answer
        return None


def answer(asset_code, question):
    a = registry.get_row(asset_code)
    if not a:
        return None
    question = " ".join((question or "").split())[:300]
    intent = _intent(question)
    facts, gaps = _asset_facts(a, intent)
    citations = sorted({r for f in facts for r in f["refs"]} | {a["asset_id"]})
    record_answer = "\n".join(f["text"] for f in facts) or "The records contain nothing that answers this question."
    if gaps:
        record_answer += "\nMissing or uncertain: " + " ".join(gaps)
    ai_text = _grounded_ai(question, facts, set(citations))
    return {"asset_id": a["asset_id"], "question": question, "intent": intent,
            "answer": ai_text or record_answer, "facts": facts, "citations": citations, "data_gaps": gaps,
            "engine": "sarvam_grounded" if ai_text else "records",
            "note": "Facts come from recorded data; AI (when used) only rephrases them. " + SAFETY}


def checklist(utility_type, iso=None, contacts=None):
    usable = [c for c in (contacts or []) if c["operational_use"]]
    demo = [c for c in (contacts or []) if c["is_synthetic"] and c["verification_status"] == "verified"]
    contact = (f"{usable[0]['organization']} ({usable[0]['label']})" if usable else
               f"{demo[0]['organization']} — fictional demo contact standing in for the verified operator line"
               if demo else "no verified operator contact is on record — find the duty officer through the department")
    cands = ", ".join(c["asset_id"] + ("" if c["path_verified"] else " (unverified path)")
                      for c in ((iso or {}).get("upstream_candidates", []) + (iso or {}).get("downstream_candidates", [])))
    steps = [{"step": "Assess the hazard on site", "detail": "Keep people and traffic clear; note electrical or "
              "structural risks. Record what you observe.", "owner": "officer"}]
    if utility_type == "gas":
        steps += [{"step": "Follow the gas operator's emergency procedure", "detail": f"Contact: {contact}. Do not "
                   "operate any gas valve; keep ignition sources away.", "owner": "gas operator"}]
    elif utility_type == "electric":
        steps += [{"step": "Contact the authorized electricity operator", "detail": f"Contact: {contact}. Only the "
                   "operator may switch or isolate a circuit.", "owner": "electricity operator"}]
    elif model.has_isolation(utility_type):
        steps += [{"step": "Contact the authorized operator", "detail": f"Contact: {contact}.", "owner": "officer"},
                  {"step": "Ask the operator to confirm the isolation point", "detail": "Recorded candidates: "
                   + (cands or "none found in the records") + ". The operator decides which, if any, to use.",
                   "owner": "operator"}]
    else:
        steps += [{"step": "Contact the responsible department", "detail": f"Contact: {contact}. No isolation "
                   "devices apply to this network type.", "owner": "officer"}]
    steps += [
        {"step": "Record the operator's decision", "detail": "Use 'Utility operator coordination' on the incident "
         "(contacted → isolation confirmed / not required).", "owner": "officer"},
        {"step": "Approve the repair plan and dispatch the crew", "detail": "Dispatch only after the operator's "
         "isolation decision is recorded.", "owner": "officer"},
        {"step": "Capture repair evidence", "detail": "Before/after photos from the same spot; complete the action "
         "with a proof photo.", "owner": "field team"},
        {"step": "Verify and watch for recurrence", "detail": "Confirm restoration with the operator; new related "
         "reports reopen the incident.", "owner": "officer"},
    ]
    return {"utility_type": utility_type, "steps": steps, "note": SAFETY}


def briefing(incident):
    ctx = matching.context(incident)
    comps = query("""SELECT public_id, category, created_at, severity FROM complaints WHERE incident_id=?
                     ORDER BY created_at""", (incident["id"],))
    facts, hypotheses, gaps = [], [], list(ctx["data_gaps"])
    if comps:
        facts.append(_fact(f"{len(comps)} report(s) of {', '.join(sorted({c['category'] for c in comps}))} between "
                           f"{comps[0]['created_at'][:16]} and {comps[-1]['created_at'][:16]} UTC.",
                           *[c["public_id"] for c in comps]))
    facts.append(_fact(f"Area: {incident['ward'] or 'unmapped'} (nearest configured ward centre, approximate).",
                       incident["public_id"]))
    pf = json.loads(incident.get("priority_factors") or "{}") or {}
    facts.append(_fact(f"Priority {incident['priority_label']} ({incident['priority_score']}/100): "
                       + "; ".join(pf.get("reasons", [])[:4]), incident["public_id"]))
    top = ctx["candidates"][:3]
    for c in top:
        facts.append(_fact(f"Candidate asset {c['asset_id']} ({c['name']}), {c['distance_m']} m from a report, "
                           f"link {c['link_status']}, {c['verification_status']}.", c["asset_id"]))
    lead = next((c for c in top if c["link_status"] == "confirmed"), None) or (top[0] if top else None)
    iso, contacts = None, []
    if lead:
        a = registry.get_row(lead["asset_id"])
        h = registry.history(a)
        for m in h["maintenance"][:3]:
            facts.append(_fact(f"{lead['asset_id']}: {m['work_type']} on {m['performed_on'][:10]} — "
                               f"{m['outcome'] or 'no outcome recorded'}.", m["record_id"], lead["asset_id"]))
        contacts = registry.contacts_for(a)
        if model.has_isolation(a["utility_type"]):
            iso = graph.isolation(lead["asset_id"])
            for s in iso["sources"]:
                facts.append(_fact(f"Recorded upstream source of {lead['asset_id']}: {s['asset_id']}.", s["asset_id"]))
            for side in ("upstream", "downstream"):
                for c in iso[f"{side}_candidates"]:
                    facts.append(_fact(f"Candidate {side} control device {c['asset_id']} "
                                       f"({'verified path' if c['path_verified'] else 'path includes unverified records'}).",
                                       c["asset_id"]))
            gaps += [w for w in iso["warnings"] if "unknown" in w or "source" in w or "loop" in w]
        if not any(c["operational_use"] for c in contacts):
            gaps.append("No verified operator contact is recorded for the lead candidate asset.")
        hypotheses.append(f"The reports may relate to {lead['asset_id']} ({lead['distance_m']} m away). This is a "
                          "hypothesis from proximity and category, not a confirmed cause.")
    for f in findings.list_findings(incident_id=incident["id"]):
        facts.append(_fact(f"Finding ({f['rule_title']}): {f['explanation']}", *f["asset_ids"]))
    coord = coordination.list_updates(incident["id"])
    facts.append(_fact(f"Operator coordination status: {coord['isolation_status'].replace('_', ' ')}.",
                       incident["public_id"]))
    citations = sorted({r for f in facts for r in f["refs"]})
    summary = (f"{incident['public_id']}: {len(comps)} related report(s) in {incident['ward'] or 'an unmapped area'}"
               + (f"; lead candidate asset {lead['asset_id']}" if lead else "; no candidate utility asset") + ".")
    ai_text = _grounded_ai("Summarize this incident briefing for the officer.", facts, set(citations))
    return {"incident_id": incident["id"], "public_id": incident["public_id"],
            "summary": ai_text or summary, "facts": facts, "hypotheses": hypotheses,
            "data_gaps": list(dict.fromkeys(gaps)), "citations": citations,
            "checklist": checklist(lead["utility_type"] if lead else (ctx["utility_types"] or ["water"])[0], iso, contacts),
            "engine": "sarvam_grounded" if ai_text else "records", "note": SAFETY}
