"""
CivicMind — complaint intake (shared by the API and the demo seeder).
"""
import json

from config import CATEGORIES, REQUIRE_ISSUE_PHOTO, CITY_NAME, CIVILIAN_LANGUAGES, MEDIA_MAX_FILES
from database import query, db, now, new_tracking_code, insert_with_public_id
from services import ai, geo, evidence, clustering, risk, voice, media as media_svc, assets as asset_svc
from services.assistant import detect_language

ASSISTANT_ENGINES = ("sarvam", "rules")


class IntakeError(Exception):
    def __init__(self, message, status=422, extra=None):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra or {}


def validate(category, description, latitude, longitude, address=None):
    if category not in CATEGORIES:
        raise IntakeError("Choose a supported category.")
    description = (description or "").strip()
    if len(description) < 10:
        raise IntakeError("Please describe the issue in at least 10 characters.")
    if len(description) > 2000:
        raise IntakeError("Description is too long (max 2000 characters).")
    if not geo.valid_coordinates(latitude, longitude):
        raise IntakeError("The location is invalid. Drop a pin on the map.")
    if not geo.in_service_area(latitude, longitude):
        raise IntakeError(f"That location is outside the {CITY_NAME} service area.")
    if address is not None and len(address) > 300:
        raise IntakeError("Address is too long (max 300 characters).")
    return description


def validate_language(language, translation_en, assistant_engine):
    """Language metadata is optional and stored beside — never instead of — the original text."""
    if language is not None and language not in CIVILIAN_LANGUAGES:
        raise IntakeError("Choose English, Hindi or Marathi.")
    translation_en = (translation_en or "").strip() or None
    if translation_en and len(translation_en) > 2000:
        raise IntakeError("The English translation is too long (max 2000 characters).")
    if assistant_engine is not None and assistant_engine not in ASSISTANT_ENGINES:
        raise IntakeError("Unknown assistant engine.")
    return translation_en


LOCATION_SOURCES = ("map", "device", "device_adjusted", "search", "search_adjusted")


def create(user, category, description, latitude, longitude, address=None, image=None,
           created_at=None, is_synthetic=False, run_followups=True,
           language=None, translation_en=None, assistant_engine=None,
           voice_id=None, location_source=None, location_accuracy_m=None, geocoded_address=None,
           media_ids=None):
    description = validate(category, description, latitude, longitude, address)
    media_ids = [m for m in (media_ids or []) if isinstance(m, str) and m]
    if len(media_ids) + (1 if image else 0) > MEDIA_MAX_FILES:
        raise IntakeError(f"You can attach at most {MEDIA_MAX_FILES} photos or videos.")
    translation_en = validate_language(language, translation_en, assistant_engine)
    if location_source is not None and location_source not in LOCATION_SOURCES:
        raise IntakeError("Unknown location source.")
    if location_accuracy_m is not None:
        try:
            location_accuracy_m = float(location_accuracy_m)
        except (TypeError, ValueError):
            raise IntakeError("Location accuracy must be a number of metres.") from None
        if not 0 <= location_accuracy_m <= 50_000:
            raise IntakeError("Location accuracy is out of range.")
    geocoded_address = (geocoded_address or "").strip()[:300] or None
    latitude, longitude = float(latitude), float(longitude)
    detected = detect_language(description)["language"]
    if translation_en and (language or detected or "en") == "en":
        translation_en = None  # nothing to translate; keep a single source of truth

    dup = query("""SELECT public_id FROM complaints WHERE user_id=? AND category=? AND description=?
                   AND created_at >= datetime('now', '-2 minutes')""",
                (user["id"], category, description), one=True)
    if dup and not created_at:
        raise IntakeError(f"You already submitted this report ({dup['public_id']}).", 409,
                          {"public_id": dup["public_id"]})
    if REQUIRE_ISSUE_PHOTO and not image and not media_ids:
        raise IntakeError("Add at least one photo or video of the issue. It helps the team find and assess the "
                          "problem.", 422)

    # Classification may also read the citizen-reviewed translation; the stored description stays original.
    cls = ai.classify_complaint(description + ("\n" + translation_en if translation_en else ""), category)
    final_category = cls["category"] if cls["category"] in CATEGORIES else category

    photo = None
    if image:
        try:
            photo = evidence.process(image, "issue", final_category)
        except evidence.EvidenceError as e:
            raise IntakeError(e.message, e.status)
        if photo["verdict"] == "rejected":
            raise IntakeError("The photo did not pass the automated checks.", 422,
                              {"reasons": photo["reasons"]})

    area = geo.map_area(latitude, longitude)
    ts = created_at or now()
    tracking = new_tracking_code()
    try:
        with db(immediate=True) as conn:
            cid, public_id = insert_with_public_id(
                conn, "complaints", "CMP",
                ["tracking_code", "user_id", "category", "description", "latitude", "longitude",
                 "address", "ward", "zone", "image_path", "image_hash", "proof_verified", "proof_check",
                 "status", "severity", "ai_category", "ai_confidence", "ai_summary", "ai_engine",
                 "is_synthetic", "created_at", "updated_at", "original_language", "detected_language",
                 "description_translation_en", "translation_engine", "assistant_engine",
                 "location_source", "location_accuracy_m", "geocoded_address"],
                [tracking, user["id"], final_category, description, latitude, longitude,
                 (address or "").strip() or None, area["ward"], area["zone"],
                 photo["stored_name"] if photo else None, photo["image_hash"] if photo else None,
                 (1 if photo["passed"] else 0) if photo else None,
                 json.dumps({k: v for k, v in photo.items() if k != "stored_name"}) if photo else None,
                 "submitted", cls["severity"], cls["category"], cls.get("confidence"),
                 cls["summary"], cls["engine"], 1 if is_synthetic else 0, ts, ts,
                 language, detected, translation_en,
                 "citizen_reviewed" if translation_en else None, assistant_engine,
                 location_source, location_accuracy_m, geocoded_address])
            if voice_id:
                try:
                    vid = voice.claim(conn, user, voice_id, cid)
                except voice.VoiceError as e:
                    raise IntakeError(e.message, e.status)
                conn.execute("UPDATE complaints SET voice_recording_id=? WHERE id=?", (vid, cid))
            attached = []
            if media_ids:
                try:
                    attached = media_svc.claim(conn, user, media_ids, cid, first_position=1 if photo else 0)
                except media_svc.MediaError as e:
                    raise IntakeError(e.message, e.status)
                first_photo = next((r for r in attached if r["kind"] == "photo"), None)
                if not photo and first_photo:
                    # The first attached photo becomes the report's main photo (used by the existing checks/views).
                    chk = json.loads(first_photo["check_json"] or "{}")
                    conn.execute("""UPDATE complaints SET image_path=?, image_hash=?, proof_verified=?, proof_check=?
                                    WHERE id=?""",
                                 (first_photo["stored_name"], first_photo["sha256"],
                                  1 if chk.get("passed") else 0, first_photo["check_json"], cid))
                    photo = dict(chk, stored_name=None)
            match = asset_svc.match_complaint(conn, cid, final_category, latitude, longitude)
            conn.execute("""INSERT INTO notifications (user_id, complaint_id, title, message, type, created_at)
                            VALUES (?,?,?,?, 'info', ?)""",
                         (user["id"], cid, "Complaint received",
                          f"Your complaint {public_id} was received and classified as {final_category} "
                          f"(severity {cls['severity']}/5). Keep your tracking code: {tracking}.", ts))
            conn.execute("""INSERT INTO activity (actor, actor_id, action, detail, complaint_id, created_at)
                            VALUES (?,?,?,?,?,?)""",
                         (user["name"], user["id"], "complaint_submitted",
                          f"{public_id} submitted ({final_category})", cid, ts))
    except Exception:
        # Attached uploads stay unclaimed (the transaction rolled back) so the citizen can retry.
        if photo and photo.get("stored_name"):
            evidence.discard(photo["stored_name"])
        raise

    placed = {"incident_id": None, "created": False}
    if run_followups:
        try:
            placed = clustering.place_complaint(cid)
        except Exception as exc:  # noqa: BLE001 — the complaint itself is safely stored
            print(f"[CivicMind] grouping skipped for {public_id}: {type(exc).__name__}")
        risk.safe_scan()

    row = query("SELECT status, incident_id FROM complaints WHERE id=?", (cid,), one=True)
    return {
        "id": cid, "public_id": public_id, "tracking_code": tracking,
        "status": row["status"], "incident_id": row["incident_id"],
        "classification": {
            "category": final_category, "severity": cls["severity"],
            "department": ai.department_for(final_category), "summary": cls["summary"],
            "engine": cls["engine"], "fallback_reason": cls.get("ai_error"),
        },
        "photo": evidence.public_check(photo) if photo else None,
        "attachments": {"photos": (1 if image else 0) + sum(r["kind"] == "photo" for r in attached),
                        "videos": sum(r["kind"] == "video" for r in attached)},
        "language": {"original": language, "detected": detected, "has_translation": bool(translation_en)},
        "area": area,
        "asset_match": {"asset_code": match["asset"]["asset_code"],
                        "type_label": asset_svc.type_label(match["asset"]["asset_type"]),
                        "distance_m": match["distance_m"]} if match else None,
        "next_step": next_step(row["status"]),
    }


NEXT_STEPS = {
    "submitted": "An officer will review your report. If similar reports arrive nearby, they will be grouped into one incident.",
    "clustered": "Your report is part of an incident awaiting an officer's response plan.",
    "in_progress": "A response plan has been approved and work is under way.",
    "resolved": "The issue was marked resolved with a proof photo. Report again if the problem returns.",
    "rejected": "This report was closed without action.",
}


def next_step(status):
    return NEXT_STEPS.get(status, "")
