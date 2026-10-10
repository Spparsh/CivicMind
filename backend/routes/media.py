"""CivicMind — report photos and videos (up to MEDIA_MAX_FILES per report).

  GET    /api/media/policy                      any signed-in user (limits + privacy note)
  POST   /api/media                             citizen; raw file bytes; type decided from the content
  DELETE /api/media/{media_id}                  citizen; own upload not yet attached to a report
  GET    /api/complaints/{id}/media/{mid}       complaint owner, officer, admin (private file)
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

import config
from auth import current_user, require_citizen, is_staff
from database import query
from routes.common import raise_from
from routes.voice_geo import _throttle
from services import media

router = APIRouter(prefix="/api", tags=["media"])
UPLOAD_PATH = "/api/media"
UPLOAD_LIMIT = config.MEDIA_MAX_VIDEO_BYTES + 64 * 1024   # used by the request-size guard in main.py


@router.get("/media/policy")
def media_policy(user: dict = Depends(current_user)):
    return media.limits()


@router.post("/media")
async def upload_media(request: Request, user: dict = Depends(require_citizen)):
    _throttle(("media", user["id"]), 20, 600)
    blob = await request.body()
    try:
        return await run_in_threadpool(media.create, user, blob)
    except media.MediaError as e:
        raise_from(e)


@router.delete("/media/{media_id}")
def remove_media(media_id: str, user: dict = Depends(require_citizen)):
    try:
        return media.remove_unclaimed(user, media_id[:64])
    except media.MediaError as e:
        raise_from(e)


@router.get("/complaints/{complaint_id}/media/{media_id}")
def complaint_media(complaint_id: int, media_id: int, user: dict = Depends(current_user)):
    c = query("SELECT user_id FROM complaints WHERE id=?", (complaint_id,), one=True)
    if not c or not (is_staff(user) or c["user_id"] == user["id"]):
        raise HTTPException(404, "File not available")
    r = query("SELECT * FROM complaint_media WHERE id=? AND complaint_id=?", (media_id, complaint_id), one=True)
    path = media.resolve(r) if r else None
    if not path:
        raise HTTPException(404, "File not available")
    return FileResponse(path, media_type=r["mime"], headers={"Cache-Control": "private, no-store"})
