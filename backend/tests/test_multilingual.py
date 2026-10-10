"""Multilingual civilian intake: assistant suggestions, language storage, preferences.

AI is disabled for the suite (conftest); provider behaviour is tested by
monkeypatching the Sarvam transport — no live call is ever made."""
import json

import requests

from database import query
from services import ai, assistant
from routes import assistant as assistant_routes
from conftest import PUNE, fresh_photo

HI_LEAK = "हमारी गली में पानी की पाइप लीक हो रही है, बहुत पानी बह रहा है"
MR_GARBAGE = "आमच्या  रस्त्यावर खूप कचरा साचला आहे\nआणि घाण वास येत आहे"


def _assist(actor, text, language="en", **extra):
    return actor.post("/api/assistant/complaint", json={"text": text, "language": language, **extra})


def setup_function(_):
    assistant_routes.reset_throttle()


def test_capabilities_are_honest_without_ai(client, make_user):
    caps = make_user("citizen").get("/api/assistant/capabilities").json()
    assert [l["code"] for l in caps["languages"]] == ["en", "hi", "mr"]
    assert caps["translation_available"] is False and caps["assistant_engine"] == "rules"
    assert caps["speech"]["available"] is False and caps["speech"]["reason"]


def test_hindi_rules_fallback_keeps_original_and_reports_no_translation(client, make_user):
    citizen = make_user("citizen")
    before = query("SELECT COUNT(*) c FROM complaints", one=True)["c"]
    r = _assist(citizen, HI_LEAK, "hi")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["original_text"] == HI_LEAK
    assert d["selected_language"] == "hi" and d["detected_language"] == "hi"
    assert d["suggestion"]["category"] == "Water Leakage" and d["suggestion"]["engine"] == "rules"
    assert d["translation"] is None and d["translation_status"] == "unavailable"
    assert "अनुवाद" in d["translation_message"]
    assert d["suggestion"]["follow_up_questions"][0] == assistant._CATEGORY_Q["Water Leakage"]["hi"]
    assert d["requires_review"] is True
    assert query("SELECT COUNT(*) c FROM complaints", one=True)["c"] == before  # suggestion only


def test_marathi_detection_and_classification(client, make_user):
    d = _assist(make_user("citizen"), MR_GARBAGE, "mr").json()
    assert d["original_text"] == MR_GARBAGE  # whitespace and newline untouched
    assert d["detected_language"] == "mr" and d["suggestion"]["category"] == "Garbage Accumulation"
    assert assistant.detect_language("रस्त्यावर मोठा खड्डा आहे")["language"] == "mr"
    assert ai._rules_classify("रस्त्यावर मोठा खड्डा आहे")["category"] == "Pothole"
    assert ai._rules_classify("yahan bahut kachra pada hai")["category"] == "Garbage Accumulation"


def test_english_needs_no_translation(client, make_user):
    d = _assist(make_user("citizen"), "The streetlight near the school gate is not working", "en").json()
    assert d["translation_status"] == "not_needed" and d["translation_message"] is None
    assert d["suggestion"]["category"] == "Streetlight Failure"


def test_assistant_validation_and_access(client, make_user):
    citizen, officer = make_user("citizen"), make_user("officer")
    assert _assist(citizen, HI_LEAK, "fr").status_code == 422
    assert _assist(citizen, "short", "en").status_code == 422
    assert _assist(officer, HI_LEAK, "hi").status_code == 403
    assert client.post("/api/assistant/complaint", json={"text": HI_LEAK, "language": "hi"}).status_code == 401


def _fake_provider(monkeypatch, payload=None, exc=None):
    monkeypatch.setattr(ai, "ai_available", lambda: True)

    def chat(messages, **kw):
        user = json.loads(messages[1]["content"])
        assert user["complaint_text"]  # the citizen text is sent as a JSON data field
        if exc:
            raise exc
        return json.dumps(payload, ensure_ascii=False)
    monkeypatch.setattr(ai, "_chat", chat)


def test_provider_output_is_validated(client, make_user, monkeypatch):
    _fake_provider(monkeypatch, {
        "detected_language": "mr", "translation_en": "There is a lot of garbage on our road and it smells.",
        "category": "Garbage Accumulation", "severity": 9, "summary_en": "Garbage pile with bad smell.",
        "urgency_indicators": ["bad smell"], "follow_up_questions": ["हे किती दिवसांपासून आहे?"],
        "mentioned_places": ["Kothrud"], "status": "resolved", "role": "admin"})
    d = _assist(make_user("citizen"), MR_GARBAGE, "mr").json()
    s = d["suggestion"]
    assert s["engine"] == "sarvam" and s["category"] == "Garbage Accumulation" and s["severity"] == 5
    assert s["mentioned_places"] == ["Kothrud"] and "status" not in s and "role" not in s
    assert d["translation"]["text"].startswith("There is a lot of garbage")
    assert d["translation"]["status"] == "draft_for_citizen_review" and d["translation_status"] == "available"
    assert d["original_text"] == MR_GARBAGE and d["requires_review"] is True


def test_unsupported_provider_category_falls_back_to_rules(client, make_user, monkeypatch):
    _fake_provider(monkeypatch, {"category": "Alien Invasion", "severity": 2, "translation_en": "Pipe leaking."})
    s = _assist(make_user("citizen"), HI_LEAK, "hi").json()["suggestion"]
    assert s["category"] == "Water Leakage" and s["category_source"] == "rules"


def test_provider_failure_falls_back(client, make_user, monkeypatch):
    _fake_provider(monkeypatch, exc=requests.Timeout("slow"))
    d = _assist(make_user("citizen"), HI_LEAK, "hi").json()
    assert d["fallback_reason"] == "timeout" and d["suggestion"]["engine"] == "rules"
    assert d["translation"] is None and d["translation_status"] == "unavailable"


def test_assistant_is_throttled_per_user(client, make_user):
    citizen = make_user("citizen")
    codes = [_assist(citizen, HI_LEAK, "hi").status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


def test_submission_stores_original_and_translation_separately(client, make_user):
    citizen, officer = make_user("citizen"), make_user("officer")
    body = {"category": "Garbage Accumulation", "description": MR_GARBAGE, "latitude": PUNE[0],
            "longitude": PUNE[1], "language": "mr", "translation_en": "Lots of garbage has piled up on our road.",
            "assistant_engine": "rules", "image": fresh_photo()}
    r = citizen.post("/api/complaints", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["language"] == {"original": "mr", "detected": "mr", "has_translation": True}
    row = query("SELECT * FROM complaints WHERE id=?", (out["id"],), one=True)
    assert row["description"] == MR_GARBAGE
    assert row["description_translation_en"] == body["translation_en"]
    assert row["original_language"] == "mr" and row["translation_engine"] == "citizen_reviewed"
    mine = citizen.get(f"/api/complaints/{out['id']}").json()
    assert mine["description"] == MR_GARBAGE and mine["language"]["translation_en"] == body["translation_en"]
    staff = officer.get(f"/api/complaints/{out['id']}").json()
    assert staff["description"] == MR_GARBAGE and staff["language"]["original"] == "mr"


def test_submission_language_validation(client, make_user):
    citizen = make_user("citizen")
    base = {"category": "Pothole", "description": "Big pothole near the bus stop on the main road",
            "latitude": PUNE[0], "longitude": PUNE[1]}
    r = citizen.post("/api/complaints", json=base | {"language": "fr", "image": fresh_photo()})
    assert r.status_code == 422
    r = citizen.post("/api/complaints", json=base | {"assistant_engine": "gpt", "image": fresh_photo()})
    assert r.status_code == 422
    r = citizen.post("/api/complaints", json=base | {"language": "en", "translation_en": "Same text", "image": fresh_photo()})
    assert r.status_code == 200
    row = query("SELECT * FROM complaints WHERE id=?", (r.json()["id"],), one=True)
    assert row["description_translation_en"] is None and row["original_language"] == "en"
    r = citizen.post("/api/complaints", json=base | {"description": "Another pothole report without language data", "image": fresh_photo()})
    assert r.status_code == 200  # language fields stay optional


def test_language_preference(client, make_user):
    citizen = make_user("citizen")
    assert citizen.post("/api/auth/preferences", json={"preferred_language": "xx"}).status_code == 422
    r = citizen.post("/api/auth/preferences", json={"preferred_language": "mr"})
    assert r.status_code == 200 and r.json()["preferred_language"] == "mr"
    assert citizen.get("/api/auth/me").json()["preferred_language"] == "mr"
