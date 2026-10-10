"""
CivicMind — civic help chatbot for the citizen portal (EN / HI / MR).

Grounding and safety:
  * Answers come from a fixed, reviewed knowledge set about how CivicMind works
    (below), plus — only for the signed-in citizen — that citizen's OWN complaint
    statuses read through an explicit query. The model never gets database,
    file or shell access, and never sees anyone else's data.
  * If an AI provider is configured it may rephrase the grounded answer. Its
    text is rejected (deterministic answer used instead) if it introduces
    phone numbers or other long digit runs, URLs or e-mail addresses that are
    not in the grounding, or is empty/oversized. It cannot invent deadlines,
    contacts or rules.
  * A description of a problem becomes a complaint DRAFT; the citizen must
    review it in the form and press Submit. The chatbot never submits.
  * Output is plain text; the UI renders it with textContent.
"""
import json
import re

from database import query
from services import ai, assistant

LANGS = ("en", "hi", "mr")
_DIGITS = re.compile(r"\d{5,}")
_URLISH = re.compile(r"(https?://|www\.|@)", re.I)

STATUS = {
    "en": {"submitted": "Submitted", "clustered": "Grouped into an incident", "in_progress": "Work in progress",
           "resolved": "Resolved", "rejected": "Closed without action"},
    "hi": {"submitted": "जमा की गई", "clustered": "घटना में शामिल", "in_progress": "काम जारी", "resolved": "हल हुई",
           "rejected": "बिना कार्रवाई बंद"},
    "mr": {"submitted": "नोंदवली", "clustered": "घटनेत समाविष्ट", "in_progress": "काम सुरू", "resolved": "सोडवली",
           "rejected": "कारवाईशिवाय बंद"},
}

# intent -> (keywords, {lang: answer})
KNOWLEDGE = {
    "emergency": (["fire", "gas leak", "smell of gas", "electrocut", "live wire", "injured", "collapse", "आग", "गैस",
                   "गॅस", "करंट", "बिजली का तार", "जखमी", "घायल"],
                  {"en": "If anyone is in immediate danger, call the national emergency number 112 first. CivicMind is "
                         "for reporting civic problems and is not an emergency service.",
                   "hi": "अगर किसी की जान को तुरंत खतरा है, तो पहले राष्ट्रीय आपातकालीन नंबर 112 पर कॉल करें। CivicMind "
                         "नागरिक समस्याएँ दर्ज करने के लिए है, यह आपातकालीन सेवा नहीं है।",
                   "mr": "कोणाच्या जीवाला तात्काळ धोका असल्यास आधी राष्ट्रीय आपत्कालीन क्रमांक 112 वर कॉल करा. CivicMind "
                         "नागरी समस्या नोंदवण्यासाठी आहे, ही आपत्कालीन सेवा नाही."}),
    "my_complaints": (["my complaint", "my report", "status of my", "my status", "मेरी शिकायत", "माझी तक्रार",
                       "माझ्या तक्रार", "मेरी रिपोर्ट"], {}),
    "how_report": (["how to report", "how do i report", "how can i report", "submit", "file a complaint", "complain",
                    "शिकायत कैसे", "शिकायत दर्ज", "तक्रार कशी", "तक्रार नोंद"],
                   {"en": "Use 'Report a civic issue': choose a category, describe the problem (type it or record up to "
                          "15 seconds), add a photo (required), set the location on the map or with 'Use my current "
                          "location', review, and submit. You will get a reference number and a private tracking code.",
                    "hi": "'नागरिक समस्या दर्ज करें' में श्रेणी चुनें, समस्या लिखें (या 15 सेकंड तक बोलकर रिकॉर्ड करें), "
                          "फ़ोटो जोड़ें (आवश्यक), मानचित्र पर या 'मेरा स्थान उपयोग करें' से जगह चुनें, जाँचें और भेजें। "
                          "आपको संदर्भ संख्या और निजी ट्रैकिंग कोड मिलेगा।",
                    "mr": "'नागरी समस्या नोंदवा' मध्ये प्रकार निवडा, समस्या लिहा (किंवा 15 सेकंदांपर्यंत बोलून रेकॉर्ड करा), "
                          "फोटो जोडा (आवश्यक), नकाशावर किंवा 'माझे ठिकाण वापरा' ने ठिकाण निवडा, तपासा आणि पाठवा. "
                          "तुम्हाला संदर्भ क्रमांक आणि खाजगी ट्रॅकिंग कोड मिळेल."}),
    "photo": (["photo", "picture", "image", "evidence", "camera", "फ़ोटो", "फोटो", "तस्वीर", "पुरावा", "प्रमाण"],
              {"en": "One clear photo of the problem is required. Use JPEG, PNG or WebP up to 5 MB. Location data inside "
                     "the photo is removed, and only you and municipal staff can see it.",
               "hi": "समस्या की एक साफ़ फ़ोटो आवश्यक है। JPEG, PNG या WebP, अधिकतम 5 MB। फ़ोटो में मौजूद स्थान की जानकारी "
                     "हटा दी जाती है और इसे केवल आप और नगर कर्मचारी देख सकते हैं।",
               "mr": "समस्येचा एक स्पष्ट फोटो आवश्यक आहे. JPEG, PNG किंवा WebP, जास्तीत जास्त 5 MB. फोटोमधील स्थानाची "
                     "माहिती काढली जाते आणि तो फक्त तुम्ही आणि महापालिका कर्मचारी पाहू शकतात."}),
    "voice": (["voice", "record", "audio", "speak", "mic", "आवाज", "रिकॉर्ड", "बोल"],
              {"en": "You can record up to 15 seconds instead of typing. When speech-to-text is available you get a "
                     "transcript to check and edit before submitting. Your recording stays private.",
               "hi": "लिखने के बजाय आप 15 सेकंड तक रिकॉर्ड कर सकते हैं। स्पीच-टू-टेक्स्ट उपलब्ध होने पर आपको लिखित रूप "
                     "मिलेगा जिसे भेजने से पहले आप जाँच और सुधार सकते हैं। रिकॉर्डिंग निजी रहती है।",
               "mr": "लिहिण्याऐवजी तुम्ही 15 सेकंदांपर्यंत रेकॉर्ड करू शकता. स्पीच-टू-टेक्स्ट उपलब्ध असल्यास तुम्हाला मजकूर "
                     "मिळेल, जो पाठवण्यापूर्वी तपासून दुरुस्त करता येतो. रेकॉर्डिंग खाजगी राहते."}),
    "track": (["track", "tracking", "code", "where is my", "ट्रैक", "मागोवा", "कोड"],
              {"en": "Open 'My reports' to see each report's status and history. You can also enter your private "
                     "tracking code in 'Track with a code'. Keep that code private.",
               "hi": "'मेरी शिकायतें' में हर शिकायत की स्थिति और इतिहास देखें। 'कोड से ट्रैक करें' में अपना निजी "
                     "ट्रैकिंग कोड भी डाल सकते हैं। यह कोड निजी रखें।",
               "mr": "'माझ्या तक्रारी' मध्ये प्रत्येक तक्रारीची स्थिती आणि इतिहास पहा. 'कोडने ट्रॅक करा' मध्ये तुमचा "
                     "खाजगी ट्रॅकिंग कोडही टाकू शकता. हा कोड खाजगी ठेवा."}),
    "time": (["how long", "when will", "deadline", "how many days", "कब तक", "कितने दिन", "केव्हा", "किती दिवस"],
             {"en": "CivicMind does not promise fixed deadlines. When the department schedules the work, the estimated "
                    "or scheduled completion appears on your report; until then it shows 'pending estimate'.",
              "hi": "CivicMind कोई तय समय-सीमा का वादा नहीं करता। विभाग काम का समय तय करने पर अनुमानित या निर्धारित "
                    "पूरा होने का समय आपकी शिकायत पर दिखेगा; तब तक 'अनुमान लंबित' दिखेगा।",
              "mr": "CivicMind ठराविक मुदतीचे आश्वासन देत नाही. विभागाने कामाचे वेळापत्रक ठरवल्यावर अंदाजित किंवा "
                    "नियोजित पूर्णतेची वेळ तुमच्या तक्रारीवर दिसेल; तोपर्यंत 'अंदाज प्रलंबित' दिसेल."}),
    "status_meaning": (["status", "what does", "meaning", "grouped", "स्थिति", "स्थिती", "मतलब", "अर्थ"],
                       {"en": "Submitted: received. Grouped: linked with similar nearby reports into one incident. Work in "
                              "progress: a plan was approved and work started. Resolved: closed with a proof photo after "
                              "officer checks — report again if the problem returns.",
                        "hi": "जमा: प्राप्त हुई। घटना में शामिल: आस-पास की समान शिकायतों के साथ जोड़ी गई। काम जारी: योजना "
                              "स्वीकृत और काम शुरू। हल: अधिकारी की जाँच और प्रमाण फ़ोटो के बाद बंद — समस्या फिर हो तो दोबारा बताएं।",
                        "mr": "नोंदवली: मिळाली. घटनेत समाविष्ट: जवळच्या समान तक्रारींसोबत जोडली. काम सुरू: आराखडा मंजूर आणि "
                              "काम सुरू. सोडवली: अधिकाऱ्याची तपासणी व पुराव्याच्या फोटोनंतर बंद — समस्या पुन्हा आल्यास पुन्हा कळवा."}),
    "location": (["location", "address", "map", "landmark", "where", "स्थान", "पता", "ठिकाण", "पत्ता", "नकाशा"],
                 {"en": "Set the exact spot by tapping the map or using 'Use my current location' (your browser asks "
                        "permission). Add a landmark so the team can find it. The map pin is what the team uses.",
                  "hi": "मानचित्र पर टैप करके या 'मेरा स्थान उपयोग करें' से सही जगह चुनें (ब्राउज़र अनुमति माँगेगा)। "
                        "पहचान चिह्न जोड़ें ताकि टीम जगह ढूँढ सके।",
                  "mr": "नकाशावर टॅप करून किंवा 'माझे ठिकाण वापरा' ने अचूक जागा निवडा (ब्राउझर परवानगी मागेल). टीमला जागा "
                        "सापडावी म्हणून खूण जोडा."}),
    "privacy": (["privacy", "who can see", "my data", "private", "गोपनीय", "निजी", "खाजगी"],
                {"en": "Only you and municipal staff can see your reports, photos and recordings. Other citizens never "
                       "see your details. Public tracking with your code shows status only.",
                 "hi": "आपकी शिकायतें, फ़ोटो और रिकॉर्डिंग केवल आप और नगर कर्मचारी देख सकते हैं। अन्य नागरिक आपकी "
                       "जानकारी नहीं देखते।",
                 "mr": "तुमच्या तक्रारी, फोटो आणि रेकॉर्डिंग फक्त तुम्ही आणि महापालिका कर्मचारी पाहू शकतात. इतर नागरिकांना "
                       "तुमची माहिती दिसत नाही."}),
    "category": (["category", "which type", "which option", "श्रेणी", "प्रकार निवड", "कौन सी"],
                 {"en": "Pick the closest category: e.g. water leak or low pressure, pothole or road damage, drainage or "
                        "sewage, garbage, streetlight, traffic signal, fallen tree, pollution, encroachment, or 'Other'. "
                        "'Check my report' can suggest one — you decide.",
                  "hi": "सबसे नज़दीकी श्रेणी चुनें: जैसे पानी का रिसाव, गड्ढा, नाली/सीवर, कचरा, स्ट्रीटलाइट, सिग्नल, गिरा पेड़, "
                        "प्रदूषण, अतिक्रमण या 'अन्य'। 'मेरी शिकायत जाँचें' सुझाव दे सकता है — निर्णय आपका।",
                  "mr": "सर्वात जवळचा प्रकार निवडा: उदा. पाणी गळती, खड्डा, गटार/सांडपाणी, कचरा, पथदिवा, सिग्नल, पडलेले झाड, "
                        "प्रदूषण, अतिक्रमण किंवा 'इतर'. 'माझी तक्रार तपासा' सूचना देऊ शकते — निर्णय तुमचा."}),
    "water": (["water", "leak", "pipe", "पानी", "पाणी", "गळती", "लीक", "पाइप", "पाईप"],
              {"en": "For a water leak: choose 'Water Leakage', photograph the water and where it comes from, pin the "
                     "exact spot and add a landmark. Do not touch valves or electrical equipment near water.",
               "hi": "पानी के रिसाव के लिए 'Water Leakage' चुनें, पानी और उसके स्रोत की फ़ोटो लें, सही जगह पिन करें और "
                     "पहचान चिह्न जोड़ें। पानी के पास वाल्व या बिजली उपकरण न छुएँ।",
               "mr": "पाणी गळतीसाठी 'Water Leakage' निवडा, पाणी आणि ते कुठून येते त्याचा फोटो घ्या, अचूक जागा पिन करा आणि "
                     "खूण जोडा. पाण्याजवळ व्हॉल्व्ह किंवा विद्युत उपकरणांना हात लावू नका."}),
    "greeting": (["hello", "hi ", "hey", "namaste", "नमस्ते", "नमस्कार"],
                 {"en": "Hello! I can explain how to report a problem, what photos are needed, how tracking works and what "
                        "your report's status means.",
                  "hi": "नमस्ते! मैं बता सकता हूँ कि समस्या कैसे दर्ज करें, कौन-सी फ़ोटो चाहिए, ट्रैकिंग कैसे होती है और "
                        "आपकी शिकायत की स्थिति का क्या अर्थ है।",
                  "mr": "नमस्कार! समस्या कशी नोंदवायची, कोणता फोटो लागतो, ट्रॅकिंग कसे चालते आणि तुमच्या तक्रारीच्या "
                        "स्थितीचा अर्थ काय, हे मी सांगू शकतो."}),
}
UNKNOWN = {"en": "I don't have reliable information about that. I can help with reporting a problem, photos, voice "
                 "recording, location, tracking and what statuses mean.",
           "hi": "इस बारे में मेरे पास भरोसेमंद जानकारी नहीं है। मैं समस्या दर्ज करने, फ़ोटो, आवाज़ रिकॉर्डिंग, स्थान, "
                 "ट्रैकिंग और स्थिति के अर्थ में मदद कर सकता हूँ।",
           "mr": "याबद्दल माझ्याकडे विश्वासार्ह माहिती नाही. समस्या नोंदवणे, फोटो, आवाज रेकॉर्डिंग, ठिकाण, ट्रॅकिंग आणि "
                 "स्थितीचा अर्थ यात मी मदत करू शकतो."}
SUGGESTIONS = {
    "en": ["How do I report a problem?", "What photo do I need?", "What does 'Grouped' mean?", "Status of my complaints"],
    "hi": ["समस्या कैसे दर्ज करें?", "कौन-सी फ़ोटो चाहिए?", "स्थिति का क्या अर्थ है?", "मेरी शिकायत की स्थिति"],
    "mr": ["समस्या कशी नोंदवायची?", "कोणता फोटो लागतो?", "स्थितीचा अर्थ काय?", "माझी तक्रार कुठे आहे?"],
}
DRAFT_NOTE = {"en": "I prepared a draft from your message. Review it in the form, add a photo and location, then submit.",
              "hi": "आपके संदेश से मैंने एक मसौदा तैयार किया है। फ़ॉर्म में जाँचें, फ़ोटो और स्थान जोड़ें, फिर भेजें।",
              "mr": "तुमच्या संदेशावरून मी एक मसुदा तयार केला आहे. फॉर्ममध्ये तपासा, फोटो आणि ठिकाण जोडा, मग पाठवा."}
_QUESTION = re.compile(r"(\?|^\s*(how|what|why|when|where|which|who|can|do|does|is|are)\b|कैसे|क्या|कब|कहाँ|कशी|काय|केव्हा|कुठे)", re.I)


def _intent(text):
    t = " " + (text or "").lower() + " "
    for name, (words, _) in KNOWLEDGE.items():
        if any(w in t for w in words):
            return name
    return None


def _my_complaints(user, lang):
    rows = query("""SELECT public_id, category, status, updated_at FROM complaints WHERE user_id=?
                    ORDER BY created_at DESC LIMIT 5""", (user["id"],))
    if not rows:
        return {"en": "You have not reported anything yet.", "hi": "आपने अभी तक कोई शिकायत दर्ज नहीं की है।",
                "mr": "तुम्ही अजून कोणतीही तक्रार नोंदवलेली नाही."}[lang], []
    lines = [f"{r['public_id']} · {r['category']} · {STATUS[lang].get(r['status'], r['status'])}" for r in rows]
    head = {"en": "Your latest reports:", "hi": "आपकी हाल की शिकायतें:", "mr": "तुमच्या अलीकडील तक्रारी:"}[lang]
    return head + "\n" + "\n".join(lines), [r["public_id"] for r in rows]


def _looks_like_problem(text):
    if len(text.strip()) < 20 or _QUESTION.search(text):
        return False
    return ai._rules_classify(text)["category"] != "Other"


def _ai_rephrase(message, grounded, lang):
    if not ai.ai_available():
        return None
    system = ("You are CivicMind's help assistant for citizens. " + ai._INJECTION_GUARD +
              f" Reply in {assistant.LANG_NAMES_EN[lang]} in at most 90 words using ONLY the 'grounding' text. Do not "
              "add phone numbers, addresses, web links, e-mail addresses, deadlines, fees or rules that are not in the "
              "grounding. If the grounding does not answer the question, say you do not have reliable information. "
              'Respond with STRICT JSON: {"reply": string}.')
    try:
        out = ai._extract_json(ai._chat([{"role": "system", "content": system},
                                         {"role": "user", "content": json.dumps(
                                             {"citizen_message": message[:500], "grounding": grounded},
                                             ensure_ascii=False)}], temperature=0.2, max_tokens=600))
        text = " ".join(str((out or {}).get("reply") or "").split())
        if not text or len(text) > 900:
            return None
        allowed_digits = set(_DIGITS.findall(grounded))
        if any(d not in allowed_digits for d in _DIGITS.findall(text)) or _URLISH.search(text):
            return None
        return text
    except Exception:  # noqa: BLE001 — deterministic answer is used
        return None


def reply(user, message, lang="en"):
    lang = lang if lang in LANGS else "en"
    message = " ".join((message or "").split())[:500]
    draft = None
    intent = _intent(message)
    refs = []
    if intent == "my_complaints":
        grounded, refs = _my_complaints(user, lang)
    elif intent == "emergency":
        grounded = KNOWLEDGE["emergency"][1][lang]
    elif _looks_like_problem(message):
        a = assistant.analyse_complaint(message, lang)
        s = a["suggestion"]
        draft = {"category": s["category"], "description": message, "language": lang,
                 "follow_up_questions": s["follow_up_questions"], "engine": s["engine"]}
        intent = "draft"
        grounded = DRAFT_NOTE[lang] + (" " + KNOWLEDGE[_intent(message) or "how_report"][1][lang]
                                       if _intent(message) in ("water",) else "")
    elif intent:
        grounded = KNOWLEDGE[intent][1][lang]
    else:
        intent, grounded = "unknown", UNKNOWN[lang]
    text = None if intent in ("my_complaints", "draft", "emergency", "unknown") else _ai_rephrase(message, grounded, lang)
    return {"reply": text or grounded, "intent": intent, "engine": "sarvam_grounded" if text else "knowledge_base",
            "draft": draft, "references": refs, "suggestions": SUGGESTIONS[lang],
            "note": "Answers come from CivicMind's help content and your own reports only."}
