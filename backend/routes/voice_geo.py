"""CivicMind — voice descriptions and reverse geocoding.

  POST /api/voice                      citizen; WAV ≤ 15 s validated on the server; returns an editable transcript
  GET  /api/voice/policy               any signed-in user (retention / provider disclosure)
  GET  /api/complaints/{id}/voice      complaint owner, officer, admin (private audio)
  GET  /api/geo/reverse?lat=&lng=      any signed-in user; opt-in provider; throttled
  GET  /api/geo/search?q=              any signed-in user; ward names offline + opt-in provider; throttled
"""
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import config
from auth import current_user, require_citizen, is_staff
from database import query
from routes.common import raise_from
from services import geocode, voice

router = APIRouter(prefix="/api", tags=["voice", "geo"])
_calls, _lock = {}, threading.Lock()


def _throttle(key, limit, window):
    with _lock:
        t = time.time()
        recent = [x for x in _calls.get(key, []) if t - x < window]
        if len(recent) >= limit:
            raise HTTPException(429, "Too many requests. Please wait a moment.")
        recent.append(t)
        _calls[key] = recent


def reset_throttle():
    with _lock:
        _calls.clear()


class VoiceIn(BaseModel):
    audio: str = Field(max_length=config.VOICE_MAX_BYTES * 4 // 3 + 64)
    language: str = Field(default="en", max_length=5)


@router.post("/voice")
def upload_voice(body: VoiceIn, user: dict = Depends(require_citizen)):
    _throttle(("voice", user["id"]), 10, 600)
    try:
        return voice.create(user, body.audio, body.language)
    except voice.VoiceError as e:
        raise_from(e)


@router.get("/voice/policy")
def voice_policy(user: dict = Depends(current_user)):
    return {"max_seconds": config.VOICE_MAX_SECONDS, "transcription_available": voice.stt_available(),
            "languages": list(voice.LANG_CODES), "disclosure": voice.DISCLOSURE,
            "reverse_geocoding_available": geocode.available(),
            "reverse_geocoding_attribution": geocode.ATTRIBUTION if geocode.available() else None}


@router.get("/complaints/{complaint_id}/voice")
def complaint_voice(complaint_id: int, user: dict = Depends(current_user)):
    c = query("SELECT user_id FROM complaints WHERE id=?", (complaint_id,), one=True)
    if not c or not (is_staff(user) or c["user_id"] == user["id"]):
        raise HTTPException(404, "Recording not available")
    r = query("SELECT stored_name FROM voice_recordings WHERE complaint_id=? ORDER BY id DESC LIMIT 1",
              (complaint_id,), one=True)
    path = voice.resolve_path(r["stored_name"]) if r else None
    if not path:
        raise HTTPException(404, "Recording not available")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})


@router.get("/geo/reverse")
def reverse(lat: float = Query(..., ge=-90, le=90), lng: float = Query(..., ge=-180, le=180),
            user: dict = Depends(current_user)):
    _throttle(("geo", user["id"]), 6, 60)
    return geocode.reverse(lat, lng)


@router.get("/geo/search")
def search(q: str = Query(..., min_length=1, max_length=120), lang: str = Query("en", max_length=5),
           user: dict = Depends(current_user)):
    _throttle(("geo", user["id"]), 6, 60)
    return geocode.search(q, lang)
