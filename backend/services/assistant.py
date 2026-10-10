"""
CivicMind — multilingual civilian complaint assistant (English / Hindi / Marathi).

It only ever produces a SUGGESTION that the citizen reviews; it never writes
to the database and never submits anything. Guarantees:

  * The citizen's original text is returned byte-for-byte as `original_text`
    and is what gets stored as the complaint description.
  * Language: the citizen's selected language is authoritative; detection is
    a deterministic script/keyword heuristic reported separately.
  * Translation to English is produced only by a configured AI provider. If
    none is available the response says so (`translation_status:
    "unavailable"`) — nothing is faked.
  * Provider output is schema-validated: unknown keys are dropped, the
    category must be a configured category, numbers are clamped and text is
    capped. On any provider failure the rule-based engine answers.
  * No location is inferred: place names the text mentions are returned as
    hints only; the citizen still sets the map pin.

Providers sit behind `AssistantProvider` so another model service can be
added without touching routes or the UI.
"""
import json
import re

import config
from config import CATEGORIES, CIVILIAN_LANGUAGES
from services import ai

LANG_NAMES_EN = {"en": "English", "hi": "Hindi", "mr": "Marathi"}

_MR_MARKERS = {"आहे", "आहेत", "नाही", "आणि", "झाले", "झाला", "झाली", "पाणी", "रस्त्यावर", "रस्ता", "खड्डा",
               "खड्डे", "मध्ये", "गळती", "येथे", "इथे", "आमच्या", "दिवसांपासून", "साचले", "गटार", "करावी",
               "आम्ही", "होत", "पण", "खूप", "लवकर"}
_HI_MARKERS = {"है", "हैं", "नहीं", "और", "में", "का", "की", "के", "पानी", "सड़क", "गड्ढा", "गड्ढे", "से", "पर",
               "रहा", "रही", "दिनों", "यहाँ", "यहां", "कूड़ा", "हमारे", "हम", "बहुत", "जल्दी", "गया", "गई"}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN = re.compile(r"[A-Za-z]")
_WORD = re.compile(r"[ऀ-ॿ]+")

# Deterministic follow-up questions (shown when the AI provider is unavailable).
_GENERIC_Q = {
    "en": ["Since when has this problem been there?", "Is anyone in danger right now?",
           "Is there a landmark near the spot?"],
    "hi": ["यह समस्या कब से है?", "क्या अभी किसी को खतरा है?", "क्या उस जगह के पास कोई पहचान चिह्न है?"],
    "mr": ["ही समस्या कधीपासून आहे?", "आत्ता कोणाला धोका आहे का?", "त्या ठिकाणाजवळ एखादी खूण आहे का?"],
}
_CATEGORY_Q = {
    "Water Leakage": {"en": "Is it clean supply water or dirty water?",
                      "hi": "क्या यह साफ नल का पानी है या गंदा पानी?",
                      "mr": "हे स्वच्छ नळाचे पाणी आहे की घाण पाणी?"},
    "Sewage Overflow": {"en": "Is the overflow entering homes or shops?",
                        "hi": "क्या गंदा पानी घरों या दुकानों में जा रहा है?",
                        "mr": "सांडपाणी घरांमध्ये किंवा दुकानांमध्ये जात आहे का?"},
    "Streetlight Failure": {"en": "How many lights are off?", "hi": "कितनी लाइटें बंद हैं?",
                            "mr": "किती दिवे बंद आहेत?"},
    "Pothole": {"en": "Is the pothole on the main road or a side lane?",
                "hi": "क्या गड्ढा मुख्य सड़क पर है या गली में?",
                "mr": "खड्डा मुख्य रस्त्यावर आहे की गल्लीत?"},
}

_UNAVAILABLE = {
    "en": "Automatic translation is not available right now. Your report will be saved exactly as you wrote it.",
    "hi": "स्वचालित अनुवाद अभी उपलब्ध नहीं है। आपकी शिकायत वैसे ही सहेजी जाएगी जैसे आपने लिखी है।",
    "mr": "स्वयंचलित भाषांतर सध्या उपलब्ध नाही. तुमची तक्रार तुम्ही लिहिली तशीच जतन केली जाईल.",
}


class AssistantError(Exception):
    pass


def detect_language(text):
    """Deterministic guess: {'language': 'en'|'hi'|'mr'|None, 'method', 'confidence'}."""
    text = text or ""
    dev, lat = len(_DEVANAGARI.findall(text)), len(_LATIN.findall(text))
    method = "script_and_keyword_heuristic"
    if dev == 0:
        if lat == 0:
            return {"language": None, "method": method, "confidence": "none"}
        # Latin script: English or romanized Hindi/Marathi — cannot tell reliably.
        return {"language": "en", "method": method, "confidence": "low"}
    words = _WORD.findall(text)
    mr = sum(1 for w in words if w in _MR_MARKERS)
    hi = sum(1 for w in words if w in _HI_MARKERS)
    if mr == hi:
        return {"language": None, "method": method, "confidence": "ambiguous_devanagari"}
    return {"language": "mr" if mr > hi else "hi", "method": method,
            "confidence": "medium" if abs(mr - hi) >= 2 else "low"}


def follow_up_questions(category, language):
    lang = language if language in _GENERIC_Q else "en"
    qs = []
    if category in _CATEGORY_Q:
        qs.append(_CATEGORY_Q[category][lang])
    return (qs + _GENERIC_Q[lang])[:3]


# --------------------------------------------------------------------------- providers
class AssistantProvider:
    name = "base"

    def available(self):
        return False

    def translation_available(self):
        return False

    def analyse(self, text, language, category_hint=None):  # pragma: no cover — interface
        raise NotImplementedError


class SarvamAssistant(AssistantProvider):
    """Uses the app's existing Sarvam chat transport (services/ai.py)."""
    name = "sarvam"

    def available(self):
        return ai.ai_available()

    def translation_available(self):
        return ai.ai_available()

    def analyse(self, text, language, category_hint=None):
        lang_name = LANG_NAMES_EN.get(language, "English")
        system = (
            "You are CivicMind's multilingual complaint assistant for an Indian municipal corporation. "
            + ai._INJECTION_GUARD +
            " The citizen may write in English, Hindi or Marathi (Devanagari or romanized). Respond with STRICT "
            'JSON only: {"detected_language": "en"|"hi"|"mr"|"other", '
            '"translation_en": faithful English translation of complaint_text without adding facts, or null if '
            'it is already English, "category": one of ' + json.dumps(CATEGORIES) + ', '
            '"severity": integer 1-5, "summary_en": one short neutral English sentence, '
            '"urgency_indicators": [up to 4 short English phrases describing danger signs actually present], '
            '"follow_up_questions": [up to 3 short questions in ' + lang_name + ' asking for missing details], '
            '"mentioned_places": [up to 3 place names or landmarks written in the text]}. '
            "Never invent a location, a number or a fact that is not in the text."
        )
        user = json.dumps({"complaint_text": text[:2000], "citizen_language": language,
                           "citizen_selected_category": category_hint if category_hint in CATEGORIES else None},
                          ensure_ascii=False)
        out = ai._extract_json(ai._chat([{"role": "system", "content": system},
                                         {"role": "user", "content": user}], temperature=0.1, max_tokens=2000))
        if not out:
            raise AssistantError("unparseable output")
        return out


PROVIDERS = [SarvamAssistant()]


def _provider():
    return next((p for p in PROVIDERS if p.available()), None)


def capabilities():
    from services import voice  # local import: voice does not depend on the assistant
    p = _provider()
    stt = voice.stt_available()
    return {
        "languages": [{"code": k, "label": v} for k, v in CIVILIAN_LANGUAGES.items()],
        "assistant_engine": p.name if p else "rules",
        "translation_available": bool(p and p.translation_available()),
        "classification": "ai_with_rules_fallback" if p else "rules",
        "speech": {"available": stt, "recording": True,
                   "max_seconds": config.VOICE_MAX_SECONDS,
                   "reason": None if stt else ("Speech-to-text is not configured; recordings are kept for staff "
                                               "and you type the description.")},
        "note": ("Suggestions only. You review and edit everything before submitting; your original words "
                 "are always kept."),
    }


# --------------------------------------------------------------------------- validation
def _validate(raw, language, fallback):
    """Keep only known fields; reject unsupported categories; clamp and cap."""
    category = ai._coerce_category(raw.get("category"))
    category_source = "ai"
    if not category:
        category, category_source = fallback["category"], "rules"
    try:
        severity = max(1, min(5, int(raw.get("severity"))))
    except (TypeError, ValueError):
        severity = fallback["severity"]
    det = raw.get("detected_language")
    det = det if det in ("en", "hi", "mr") else None
    translation = ai._s(raw.get("translation_en"), 2000) or None
    return {
        "category": category, "category_source": category_source, "severity": severity,
        "summary": ai._s(raw.get("summary_en"), 300) or fallback["summary"],
        "urgency_indicators": ai._str_list(raw.get("urgency_indicators"), 4, 120),
        "follow_up_questions": ai._str_list(raw.get("follow_up_questions"), 3, 200)
        or follow_up_questions(category, language),
        "mentioned_places": ai._str_list(raw.get("mentioned_places"), 3, 80),
        "detected_language": det, "translation_en": translation,
    }


def analyse_complaint(text, language="en", category_hint=None):
    """Suggestion for the citizen to review. Never raises for provider problems; never persists."""
    original = text if isinstance(text, str) else ""
    language = language if language in CIVILIAN_LANGUAGES else "en"
    detection = detect_language(original)
    rules = ai._rules_classify(original, category_hint)
    rules_urgent = [w for w in ai.URGENCY_WORDS + ai.URGENCY_WORDS_INDIC if w in original.lower()]
    needs_translation = language != "en" or detection["language"] in ("hi", "mr")

    suggestion = {
        "category": rules["category"], "category_source": "rules", "severity": rules["severity"],
        "department": rules["department"], "summary": rules["summary"],
        "urgency_indicators": rules_urgent[:4], "follow_up_questions": follow_up_questions(rules["category"], language),
        "mentioned_places": [], "engine": "rules", "confidence_kind": "keyword_match_strength",
    }
    translation, translation_status, fallback_reason = None, ("unavailable" if needs_translation else "not_needed"), None

    provider = _provider()
    if provider:
        try:
            v = _validate(provider.analyse(original, language, category_hint), language, rules)
            suggestion.update({k: v[k] for k in ("category", "category_source", "severity", "summary",
                                                 "urgency_indicators", "follow_up_questions", "mentioned_places")})
            suggestion["department"] = ai.department_for(v["category"])
            suggestion["engine"] = provider.name
            suggestion["confidence_kind"] = None
            if v["detected_language"]:
                detection = dict(detection, ai_language=v["detected_language"])
            if needs_translation:
                if v["translation_en"]:
                    translation = {"text": v["translation_en"], "language": "en", "engine": provider.name,
                                   "status": "draft_for_citizen_review"}
                    translation_status = "available"
                else:
                    translation_status = "unavailable"
        except Exception as e:  # noqa: BLE001 — any provider failure falls back to rules
            fallback_reason = ai._error_kind(e)

    return {
        "original_text": original,
        "selected_language": language,
        "detected_language": detection["language"],
        "detection": detection,
        "language_mismatch": bool(detection["language"] and detection["language"] != language),
        "translation": translation,
        "translation_status": translation_status,
        "translation_message": _UNAVAILABLE[language] if translation_status == "unavailable" else None,
        "suggestion": suggestion,
        "fallback_reason": fallback_reason,
        "requires_review": True,
        "location_note": "Place names are hints only. Set the exact location with the map pin.",
    }
