"""
CivicMind — real-time status stream (Server-Sent Events).

The citizen dashboard subscribes to /api/stream?token=<token>. EventSource
cannot send headers, so the token travels in the query string; the server
never logs it (see main.py access-log filter). The stream contains ONLY the
caller's own complaints and notifications. It polls the database every few
seconds, closes after ~10 minutes (the browser reconnects) and stops as soon
as the client disconnects. If SSE fails, the page falls back to polling.
"""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from auth import user_from_token
from database import query

router = APIRouter(prefix="/api", tags=["realtime"])
POLL_SECONDS = 4


def _snapshot(user_id):
    return {
        "complaints": query("""SELECT public_id, status, incident_id, updated_at FROM complaints
                               WHERE user_id=? ORDER BY id DESC LIMIT 50""", (user_id,)),
        "notifications": query("""SELECT id, title, message, type, created_at FROM notifications
                                  WHERE user_id=? ORDER BY id DESC LIMIT 10""", (user_id,)),
        "unread": query("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0",
                        (user_id,), one=True)["c"],
    }


@router.get("/stream")
async def stream(request: Request, token: str = ""):
    user = await run_in_threadpool(user_from_token, token)
    if not user:
        async def denied():
            yield "event: denied\ndata: {\"detail\":\"unauthorized\"}\n\n"
        return StreamingResponse(denied(), media_type="text/event-stream", status_code=401)

    user_id = user["id"]

    async def gen():
        last = None
        yield "retry: 5000\n\n"
        for _ in range(150):  # ~10 minutes per connection; the browser reconnects
            if await request.is_disconnected():
                break
            snap = await run_in_threadpool(_snapshot, user_id)
            blob = json.dumps(snap, sort_keys=True)
            if blob != last:
                last = blob
                yield f"event: update\ndata: {blob}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
