"""
CivicMind — report attachments: up to MEDIA_MAX_FILES photos or short videos.

Upload first, attach later: each file is POSTed as raw bytes to /api/media,
validated and stored privately, and the citizen receives a single-use token.
The complaint claims the tokens inside its own transaction.

The server decides the type from the file's bytes, never from the browser:
  * photos (JPEG / PNG / WebP) go through services.evidence — re-encoded to
    JPEG, which removes EXIF/GPS, plus the blank / re-use checks;
  * videos must be MP4 / MOV / 3GP (ISO base media) or WebM. The container is
    parsed: a video track must exist and, when the file declares its length,
    it must be ≤ MEDIA_MAX_VIDEO_SECONDS. Size is always capped.
    MP4/MOV location tags (©xyz, loci, Apple "location" keys) are blanked in
    place. Other embedded metadata is NOT removed (no re-encoding is done).

Unattached uploads are deleted after MEDIA_UNLINKED_TTL_HOURS.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import struct
import uuid
from datetime import timedelta

import config
from database import query, db, now, parse_ts, utcnow
from services import evidence

_VIDEO_NAME_RE = re.compile(r"^video-[0-9a-f]{32}\.(mp4|mov|3gp|webm)$")
DISCLOSURE = ("Photos and videos are stored privately with your report and are visible only to you and municipal "
              "staff. Location data is removed from photos and from MP4/MOV location tags; other details recorded "
              "inside a video file may remain.")


class MediaError(Exception):
    def __init__(self, message, status=422, extra=None):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra or {}


def limits():
    return {"max_files": config.MEDIA_MAX_FILES, "max_photo_mb": config.MAX_IMAGE_BYTES // (1024 * 1024),
            "max_video_mb": config.MEDIA_MAX_VIDEO_BYTES // (1024 * 1024),
            "max_video_seconds": config.MEDIA_MAX_VIDEO_SECONDS, "disclosure": DISCLOSURE}


def _media_dir():
    path = os.path.join(config.EVIDENCE_DIR, "media")
    os.makedirs(path, exist_ok=True)
    return path


def sniff(blob):
    """Return 'jpeg'|'png'|'webp'|'isobmff'|'webm' from the file's own bytes, or None."""
    if blob[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    if blob[4:8] == b"ftyp":
        return "isobmff"
    if blob[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    return None


# --------------------------------------------------------------------------- MP4 / MOV / 3GP
_DAMAGED = "The video file is damaged or incomplete."
_ISO_BRANDS = {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ",
               b"qt  ", b"3gp4", b"3gp5", b"3gp6", b"3g2a", b"MSNV", b"dash", b"mmp4"}


def _boxes(buf, start, end):
    i = start
    while i + 8 <= end:
        size = int.from_bytes(buf[i:i + 4], "big")
        typ = bytes(buf[i + 4:i + 8])
        hdr = 8
        if size == 1:
            if i + 16 > end:
                raise MediaError(_DAMAGED)
            size, hdr = int.from_bytes(buf[i + 8:i + 16], "big"), 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            raise MediaError(_DAMAGED)
        yield typ, i, i + hdr, i + size
        i += size


def _child(buf, start, end, typ):
    return next(((s, e) for t, _, s, e in _boxes(buf, start, end) if t == typ), None)


def _meta_payload(buf, s, e):
    """ISO 'meta' is a full box (4 extra bytes); QuickTime 'meta' is a plain container."""
    return s if buf[s + 4:s + 8] == b"hdlr" else s + 4


def _blank_locations(buf, start, end, keys=None):
    """Overwrite location payloads in place (sizes unchanged, so the file stays valid)."""
    removed = 0
    for typ, _, s, e in list(_boxes(buf, start, end)):
        if typ == b"\xa9xyz" and e - s > 4:      # keep the 2-byte length + 2-byte language, blank the text
            buf[s + 4:e] = b" " * (e - s - 4)
            removed += 1
        elif typ == b"loci" and e - s > 6:       # keep version/flags + language; zero name, role, coordinates
            buf[s + 6:e] = b"\x00" * (e - s - 6)
            removed += 1
        elif typ in (b"moov", b"trak", b"udta"):
            removed += _blank_locations(buf, s, e)
        elif typ == b"meta":
            p = _meta_payload(buf, s, e)
            names = {}
            k = _child(buf, p, e, b"keys")
            if k:
                count = int.from_bytes(buf[k[0] + 4:k[0] + 8], "big")
                j = k[0] + 8
                for idx in range(1, count + 1):
                    if j + 8 > k[1]:
                        break
                    ksize = int.from_bytes(buf[j:j + 4], "big")
                    if ksize < 8 or j + ksize > k[1]:
                        break
                    names[idx] = bytes(buf[j + 8:j + ksize]).decode("latin-1", "replace")
                    j += ksize
            ilst = _child(buf, p, e, b"ilst")
            if ilst:
                for ityp, _, is_, ie in _boxes(buf, ilst[0], ilst[1]):
                    name = names.get(int.from_bytes(ityp, "big"), "") if names else ""
                    if ityp == b"\xa9xyz" or "location" in name.lower():
                        d = _child(buf, is_, ie, b"data")
                        if d and d[1] - d[0] > 8:
                            buf[d[0] + 8:d[1]] = b" " * (d[1] - d[0] - 8)
                            removed += 1
    return removed


def _parse_isobmff(buf):
    major = bytes(buf[8:12])
    compat = {bytes(buf[i:i + 4]) for i in range(16, min(int.from_bytes(buf[0:4], "big"), 64), 4)}
    if major not in _ISO_BRANDS and not (compat & _ISO_BRANDS):
        raise MediaError("Unsupported video format. Use MP4, MOV or WebM.", 415)
    moov = _child(buf, 0, len(buf), b"moov")
    if not moov:
        raise MediaError(_DAMAGED)
    duration = None
    mvhd = _child(buf, moov[0], moov[1], b"mvhd")
    if mvhd:
        p = mvhd[0]
        if buf[p] == 1:
            scale, dur = int.from_bytes(buf[p + 20:p + 24], "big"), int.from_bytes(buf[p + 24:p + 32], "big")
            unknown = dur == (1 << 64) - 1
        else:
            scale, dur = int.from_bytes(buf[p + 12:p + 16], "big"), int.from_bytes(buf[p + 16:p + 20], "big")
            unknown = dur == (1 << 32) - 1
        if scale and not unknown and dur:
            duration = dur / scale
    has_video = False
    for typ, _, s, e in _boxes(buf, moov[0], moov[1]):
        if typ != b"trak":
            continue
        mdia = _child(buf, s, e, b"mdia")
        hdlr = mdia and _child(buf, mdia[0], mdia[1], b"hdlr")
        if hdlr and bytes(buf[hdlr[0] + 8:hdlr[0] + 12]) == b"vide":
            has_video = True
    if not has_video:
        raise MediaError("The file has no video track.", 415)
    _blank_locations(buf, 0, len(buf))
    if major == b"qt  ":
        return "video/quicktime", "mov", duration
    if major.startswith(b"3g"):
        return "video/3gpp", "3gp", duration
    return "video/mp4", "mp4", duration


# --------------------------------------------------------------------------- WebM (EBML)
def _vint(buf, i, keep_marker=False):
    if i >= len(buf) or buf[i] == 0:
        raise MediaError(_DAMAGED)
    first, length, mask = buf[i], 1, 0x80
    while not first & mask:
        mask >>= 1
        length += 1
    if length > 8 or i + length > len(buf):
        raise MediaError(_DAMAGED)
    value = first if keep_marker else first & (mask - 1)
    for k in range(1, length):
        value = (value << 8) | buf[i + k]
    unknown = not keep_marker and value == (1 << (7 * length)) - 1
    return value, length, unknown


def _element(buf, i):
    eid, ilen, _ = _vint(buf, i, keep_marker=True)
    size, slen, unknown = _vint(buf, i + ilen)
    start = i + ilen + slen
    return eid, start, (None if unknown else start + size)


def _uint(buf, s, e):
    return int.from_bytes(buf[s:e], "big") if e - s <= 8 else 0


def _parse_webm(buf):
    eid, s, e = _element(buf, 0)
    if eid != 0x1A45DFA3 or e is None or e > len(buf):
        raise MediaError(_DAMAGED)
    doctype = None
    i = s
    while i < e:
        cid, cs, ce = _element(buf, i)
        if ce is None:
            raise MediaError(_DAMAGED)
        if cid == 0x4282:
            doctype = bytes(buf[cs:ce]).rstrip(b"\x00")
        i = ce
    if doctype != b"webm":
        raise MediaError("Unsupported video format. Use MP4, MOV or WebM.", 415)
    sid, ss, se = _element(buf, e)
    if sid != 0x18538067:
        raise MediaError(_DAMAGED)
    se = min(se or len(buf), len(buf))
    scale, raw_duration, has_video = 1_000_000, None, False
    i = ss
    while i < se:
        cid, cs, ce = _element(buf, i)
        if cid == 0x1F43B675 or ce is None:   # first Cluster (media data) or unknown size: header is over
            break
        ce = min(ce, se)
        if cid == 0x1549A966:                   # Info
            j = cs
            while j < ce:
                fid, fs, fe = _element(buf, j)
                if fe is None:
                    break
                if fid == 0x2AD7B1:
                    scale = _uint(buf, fs, fe) or scale
                elif fid == 0x4489 and fe - fs in (4, 8):
                    raw_duration = struct.unpack(">f" if fe - fs == 4 else ">d", bytes(buf[fs:fe]))[0]
                j = fe
        elif cid == 0x1654AE6B:                 # Tracks
            j = cs
            while j < ce:
                tid, ts, te = _element(buf, j)
                if te is None:
                    break
                if tid == 0xAE:                 # TrackEntry
                    k = ts
                    while k < te:
                        fid, fs, fe = _element(buf, k)
                        if fe is None:
                            break
                        if fid == 0x83 and _uint(buf, fs, fe) == 1:
                            has_video = True
                        k = fe
                j = te
        i = ce
    if not has_video:
        raise MediaError("The file has no video track.", 415)
    duration = None
    if raw_duration is not None and raw_duration == raw_duration and raw_duration > 0:  # not NaN
        duration = raw_duration * scale / 1e9
    return "video/webm", "webm", duration


def parse_video(blob):
    """Validate a video. Returns (bytes_after_scrub, mime, ext, duration_s_or_None)."""
    if len(blob) > config.MEDIA_MAX_VIDEO_BYTES:
        raise MediaError(f"Video is too large (max {config.MEDIA_MAX_VIDEO_BYTES // (1024 * 1024)} MB).", 413)
    if len(blob) < 1024:
        raise MediaError("The video is empty or unreadable.")
    buf = bytearray(blob)
    kind = sniff(blob)
    try:
        if kind == "isobmff":
            mime, ext, duration = _parse_isobmff(buf)
        elif kind == "webm":
            mime, ext, duration = _parse_webm(buf)
        else:
            raise MediaError("Unsupported video format. Use MP4, MOV or WebM.", 415)
    except (IndexError, struct.error, OverflowError):
        raise MediaError(_DAMAGED) from None
    if duration is not None:
        if duration > config.MEDIA_MAX_VIDEO_SECONDS + 0.5:
            raise MediaError(f"Video is longer than {int(config.MEDIA_MAX_VIDEO_SECONDS)} seconds. "
                             "Trim it or record a shorter clip.")
        duration = round(duration, 1)
    return bytes(buf), mime, ext, duration


# --------------------------------------------------------------------------- upload / claim / view
def create(user, blob):
    if not blob:
        raise MediaError("No file was received.")
    purge()
    kind = sniff(blob)
    token = secrets.token_urlsafe(18)
    if kind in ("jpeg", "png", "webp"):
        if len(blob) > config.MAX_IMAGE_BYTES:
            raise MediaError(f"Photo is too large (max {config.MAX_IMAGE_BYTES // (1024 * 1024)} MB).", 413)
        sha = hashlib.sha256(blob).hexdigest()
        if query("SELECT 1 FROM complaint_media WHERE sha256=? LIMIT 1", (sha,), one=True):
            raise MediaError("This exact photo was already uploaded.", 422)
        data_url = f"data:image/{kind};base64," + base64.b64encode(blob).decode("ascii")
        try:
            check = evidence.process(data_url, "issue")
        except evidence.EvidenceError as e:
            raise MediaError(e.message, e.status) from None
        if check["verdict"] == "rejected":
            raise MediaError("The photo did not pass the automated checks.", 422, {"reasons": check["reasons"]})
        row = dict(kind="photo", stored_name=check["stored_name"], mime="image/jpeg", bytes=len(blob),
                   width=check["meta"]["width"], height=check["meta"]["height"], duration_s=None, sha256=sha,
                   check_json=json.dumps({k: v for k, v in check.items() if k != "stored_name"}))
        public = {"check": evidence.public_check(check)}
    elif kind in ("isobmff", "webm"):
        clean, mime, ext, duration = parse_video(blob)
        sha = hashlib.sha256(blob).hexdigest()
        if query("SELECT 1 FROM complaint_media WHERE sha256=? LIMIT 1", (sha,), one=True):
            raise MediaError("This exact video was already uploaded.", 422)
        name = f"video-{uuid.uuid4().hex}.{ext}"
        path = os.path.join(_media_dir(), name)
        with open(path + ".part", "wb") as fh:
            fh.write(clean)
        os.replace(path + ".part", path)
        row = dict(kind="video", stored_name=name, mime=mime, bytes=len(clean), width=None, height=None,
                   duration_s=duration, sha256=sha, check_json=None)
        public = {"duration_known": duration is not None}
    else:
        raise MediaError("Unsupported file. Use a JPEG, PNG or WebP photo, or an MP4, MOV or WebM video.", 415)
    try:
        with db() as conn:
            conn.execute("""INSERT INTO complaint_media (token, user_id, kind, stored_name, mime, bytes, width, height,
                            duration_s, sha256, check_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (token, user["id"], row["kind"], row["stored_name"], row["mime"], row["bytes"], row["width"],
                          row["height"], row["duration_s"], row["sha256"], row["check_json"], now()))
    except Exception:
        _remove_file(row)
        raise
    return {"media_id": token, "kind": row["kind"], "mime": row["mime"], "bytes": row["bytes"],
            "duration_s": row["duration_s"], **public, "limits": limits()}


def _resolve(row):
    if row["kind"] == "photo":
        return evidence.resolve_path(row["stored_name"])
    if not _VIDEO_NAME_RE.match(row["stored_name"] or ""):
        return None
    base = os.path.realpath(_media_dir())
    path = os.path.realpath(os.path.join(base, row["stored_name"]))
    return path if path.startswith(base + os.sep) and os.path.isfile(path) else None


def resolve(row):
    return _resolve(row)


def _remove_file(row):
    path = _resolve(row)
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def remove_unclaimed(user, token):
    r = query("SELECT * FROM complaint_media WHERE token=?", (token,), one=True)
    if not r or r["user_id"] != user["id"] or r["complaint_id"] is not None:
        raise MediaError("That file is not available.", 404)
    with db() as conn:
        conn.execute("DELETE FROM complaint_media WHERE id=? AND complaint_id IS NULL", (r["id"],))
    _remove_file(r)
    return {"removed": True}


def claim(conn, user, tokens, complaint_id, first_position=0):
    """Attach uploaded files to a new complaint. Returns the claimed rows in order."""
    if len(set(tokens)) != len(tokens):
        raise MediaError("The same file was attached twice.", 409)
    cut = utcnow() - timedelta(hours=config.MEDIA_UNLINKED_TTL_HOURS)
    rows = []
    for pos, token in enumerate(tokens, start=first_position):
        r = conn.execute("SELECT * FROM complaint_media WHERE token=?", (token,)).fetchone()
        created = parse_ts(r["created_at"]) if r else None
        if not r or r["user_id"] != user["id"] or r["complaint_id"] is not None or not created or created < cut:
            raise MediaError("One of the attached files is no longer available. Remove it and add it again.", 409)
        conn.execute("UPDATE complaint_media SET complaint_id=?, position=?, linked_at=? WHERE id=?",
                     (complaint_id, pos, now(), r["id"]))
        rows.append(r)
    return rows


def view(row, main_photo=None):
    return {"id": row["id"], "kind": row["kind"], "mime": row["mime"], "duration_s": row["duration_s"],
            "width": row["width"], "height": row["height"], "position": row["position"],
            "is_main_photo": row["kind"] == "photo" and row["stored_name"] == main_photo,
            "url": f"/api/complaints/{row['complaint_id']}/media/{row['id']}"}


def for_complaint(complaint_id):
    c = query("SELECT image_path FROM complaints WHERE id=?", (complaint_id,), one=True)
    main = c["image_path"] if c else None
    return [view(r, main) for r in query("SELECT * FROM complaint_media WHERE complaint_id=? ORDER BY position, id",
                                         (complaint_id,))]


def purge():
    """Delete uploads never attached to a report within the time limit."""
    cut = (utcnow() - timedelta(hours=config.MEDIA_UNLINKED_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = query("SELECT * FROM complaint_media WHERE complaint_id IS NULL AND created_at < ?", (cut,))
    if not rows:
        return 0
    with db() as conn:
        for r in rows:
            conn.execute("DELETE FROM complaint_media WHERE id=? AND complaint_id IS NULL", (r["id"],))
    for r in rows:
        if not query("SELECT 1 FROM complaints WHERE image_path=?", (r["stored_name"],), one=True):
            _remove_file(r)
    return len(rows)
