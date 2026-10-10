"""
CivicMind — photo evidence intake and automated checks.

What this module does:
  1. Decodes a base64 data URL strictly and enforces a byte limit.
  2. Validates the ACTUAL content with Pillow (JPEG / PNG / WebP only),
     rejects malformed images and decompression bombs.
  3. Re-encodes to JPEG, which strips EXIF metadata (including GPS) and any
     payload appended to the file.
  4. Stores the result in a PRIVATE directory under a server-generated name.
     Nothing in that directory is served statically; files are only returned
     by authorised API endpoints.
  5. Runs deterministic checks: blank/uniform image, very low resolution,
     tiny file, exact re-use of an earlier photo, resolution photo identical
     to the original issue photo.

What it does NOT do: it cannot establish that a physical repair happened.
The verdict means "the file passed automated integrity checks", nothing more.
"""
import base64
import binascii
import hashlib
import io
import os
import re
import uuid

from PIL import Image, ImageOps, ImageStat

from config import EVIDENCE_DIR, LEGACY_UPLOAD_DIR, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS
from database import query
from services import ai

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
_DATA_URL_RE = re.compile(r"^data:image/(jpeg|jpg|png|webp);base64,", re.I)
_STORED_NAME_RE = re.compile(r"^[a-z]{3,12}-[0-9a-f]{32}\.jpg$")
CHECK_SCOPE_NOTE = ("Automated file checks only (format, quality, re-use). "
                    "This is not an independent field inspection.")


class EvidenceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _decode(data_url: str) -> bytes:
    if not isinstance(data_url, str) or not data_url:
        raise EvidenceError("No photo was provided.")
    m = _DATA_URL_RE.match(data_url[:40])
    if not m:
        raise EvidenceError("Unsupported file type. Upload a JPEG, PNG or WebP photo.", 415)
    payload = data_url[m.end():]
    if len(payload) > (MAX_IMAGE_BYTES * 4) // 3 + 8:
        raise EvidenceError(f"Photo is too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB).", 413)
    try:
        blob = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise EvidenceError("The photo data is corrupted.")
    if len(blob) > MAX_IMAGE_BYTES:
        raise EvidenceError(f"Photo is too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB).", 413)
    if len(blob) < 100:
        raise EvidenceError("The photo is empty or unreadable.")
    return blob


def _load(blob: bytes):
    """Return (rgb_image, format). Raises EvidenceError for anything unsafe."""
    try:
        probe = Image.open(io.BytesIO(blob))
        fmt = probe.format
        if fmt not in ALLOWED_FORMATS:
            raise EvidenceError("Unsupported image format. Upload a JPEG, PNG or WebP photo.", 415)
        w, h = probe.size
        if w * h > MAX_IMAGE_PIXELS:
            raise EvidenceError("Image dimensions are too large.", 413)
        probe.verify()  # structural check
        im = Image.open(io.BytesIO(blob))  # verify() invalidates the object
        im.load()
        im = ImageOps.exif_transpose(im)
        return im.convert("RGB"), fmt
    except EvidenceError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise EvidenceError("Image dimensions are too large.", 413)
    except Exception:
        raise EvidenceError("The file is not a valid image.")


def _seen_before(sha):
    return bool(query(
        """SELECT 1 FROM complaints WHERE image_hash=:h OR resolution_image_hash=:h
           UNION SELECT 1 FROM actions WHERE image_hash=:h
           UNION SELECT 1 FROM resolutions WHERE image_hash=:h
           UNION SELECT 1 FROM work_order_evidence WHERE image_hash=:h
           UNION SELECT 1 FROM complaint_media WHERE sha256=:h LIMIT 1""", {"h": sha}, one=True))


def _flags(meta, kind, issue_hashes):
    flags, hard_fail = [], False
    if meta["width"] < 200 or meta["height"] < 200:
        flags.append("Very low resolution — hard to review")
    if meta["std"] < 8:
        flags.append("Image looks blank or uniform")
        hard_fail = True
    if meta["bytes"] < 8000:
        flags.append("Image file is unusually small")
    if kind == "resolution" and meta["sha256"] in (issue_hashes or []):
        flags.append("Proof photo is identical to an original issue photo")
        hard_fail = True
    elif _seen_before(meta["sha256"]):
        flags.append("This exact image was already submitted before")
        hard_fail = True
    return flags, hard_fail


def process(data_url: str, kind: str, category: str = None, issue_hashes=None):
    """Validate + store one photo. `kind` is 'issue' or 'resolution'.

    Returns a result dict. The file is only written when verdict != 'rejected'.
    Raises EvidenceError for unusable input (bad type, too big, corrupt).
    """
    blob = _decode(data_url)
    im, fmt = _load(blob)
    stat = ImageStat.Stat(im.convert("L"))
    meta = {"bytes": len(blob), "width": im.size[0], "height": im.size[1], "format": fmt,
            "mean": round(stat.mean[0], 2), "std": round(stat.stddev[0], 2),
            "sha256": hashlib.sha256(blob).hexdigest()}
    flags, hard_fail = _flags(meta, kind, issue_hashes)
    verdict = "rejected" if hard_fail else ("needs_review" if flags else "passed")
    result = {
        "verdict": verdict,
        "passed": verdict == "passed",
        "reasons": flags or ["Passed automated file checks"],
        "note": CHECK_SCOPE_NOTE,
        "engine": "automated_checks",
        "image_hash": meta["sha256"],
        "meta": {k: meta[k] for k in ("bytes", "width", "height", "format")},
        "stored_name": None,
        "observation": None,
    }
    if hard_fail:
        return result
    result["stored_name"] = _store(im, kind)
    # Optional, informational only — never alters the verdict.
    result["observation"] = ai.vision_observation(data_url, kind, category)
    return result


def _store(im, kind):
    name = f"{re.sub('[^a-z]', '', kind)[:12] or 'img'}-{uuid.uuid4().hex}.jpg"
    final = os.path.join(EVIDENCE_DIR, name)
    tmp = final + ".part"
    try:
        im.save(tmp, format="JPEG", quality=88, optimize=True)  # no EXIF written
        os.replace(tmp, final)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise EvidenceError("Could not store the photo. Please try again.", 500)
    return name


def discard(stored_name):
    """Remove a stored file (used when the surrounding transaction fails)."""
    path = resolve_path(stored_name)
    if path and path.startswith(EVIDENCE_DIR):
        try:
            os.remove(path)
        except OSError:
            pass


def resolve_path(stored):
    """Map a DB value to a real file path, or None. Accepts current stored
    names and legacy '/uploads/<name>' values; rejects anything else, so a
    crafted value can never escape the evidence directories."""
    if not stored or not isinstance(stored, str):
        return None
    name = os.path.basename(stored.replace("\\", "/"))
    legacy = re.match(r"^[a-z]{2,12}-[0-9a-f]{32}\.(jpg|jpeg|png|webp)$", name)
    if not (_STORED_NAME_RE.match(name) or legacy):
        return None
    for base in (EVIDENCE_DIR, LEGACY_UPLOAD_DIR):
        path = os.path.realpath(os.path.join(base, name))
        if path.startswith(os.path.realpath(base) + os.sep) and os.path.isfile(path):
            return path
    return None


def public_check(check):
    """Strip internal fields before sending a check result to a client."""
    if not isinstance(check, dict):
        return None
    verdict = {"verified": "passed", "flagged": "rejected"}.get(check.get("verdict"), check.get("verdict"))
    return {"verdict": verdict, "reasons": check.get("reasons") or [],
            "note": check.get("note") or CHECK_SCOPE_NOTE,
            "engine": check.get("engine"), "observation": check.get("observation")}
