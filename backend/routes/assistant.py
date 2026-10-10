"""CivicMind — civilian complaint assistant (suggestions only; nothing is persisted here).

  GET  /api/assistant/capabilities   any signed-in user
  POST /api/assistant/complaint      citizen; per-user throttle (AI calls cost money)
"""
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import current_user, require_citizen
from config import CIVILIAN_LANGUAGES
from services import assistant, chatbot

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

_WINDOW_S, _MAX_CALLS = 600, 20
_calls, _lock = {}, threading.Lock()


def _throttle(user_id):
    with _lock:
        now = time.time()
        recent = [t for t in _calls.get(user_id, []) if now - t < _WINDOW_S]
        if len(recent) >= _MAX_CALLS:
            raise HTTPException(429, "You have used the assistant many times in a short period. "
                                     "Please wait a few minutes or fill in the form directly.")
        recent.append(now)
        _calls[user_id] = recent


def reset_throttle():
    with _lock:
        _calls.clear()


class AssistIn(BaseModel):
    text: str = Field(min_length=10, max_length=2000)
    language: str = Field(default="en", max_length=5)
    category_hint: str | None = Field(default=None, max_length=60)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    language: str = Field(default="en", max_length=5)


@router.post("/chat")
def chat(body: ChatIn, user: dict = Depends(require_citizen)):
    if body.language not in CIVILIAN_LANGUAGES:
        raise HTTPException(422, {"message": "Unsupported language.", "allowed": list(CIVILIAN_LANGUAGES)})
    _throttle(("chat", user["id"]))
    return chatbot.reply(user, body.message, body.language)


@router.get("/capabilities")
def capabilities(user: dict = Depends(current_user)):
    return assistant.capabilities()


@router.post("/complaint")
def assist(body: AssistIn, user: dict = Depends(require_citizen)):
    if body.language not in CIVILIAN_LANGUAGES:
        raise HTTPException(422, {"message": "Unsupported language.", "allowed": list(CIVILIAN_LANGUAGES)})
    if len(body.text.strip()) < 10:
        raise HTTPException(422, "Please describe the issue in at least 10 characters.")
    _throttle(user["id"])
    return assistant.analyse_complaint(body.text, body.language, body.category_hint)
