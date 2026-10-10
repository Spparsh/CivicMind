"""Up to 3 photos/videos per report, and address search for the report location."""
import base64
import os
import struct
from datetime import timedelta

import pytest

import config
from database import db, query, utcnow
from routes import voice_geo
from services import geocode, media
from conftest import PUNE, file_complaint, fresh_photo


@pytest.fixture(autouse=True)
def _reset():
    voice_geo.reset_throttle()
    geocode._cache.clear()
    yield


# --------------------------------------------------------------------------- synthetic media files
def _box(t, payload):
    return struct.pack(">I", 8 + len(payload)) + t + payload


GPS = b"+18.5204+073.8567/"


def mp4(seconds=5, handler=b"vide", brand=b"isom", gps=True, mvhd_version=0):
    ftyp = _box(b"ftyp", brand + b"\0\0\x02\0" + b"isomiso2avc1mp41")
    if mvhd_version == 1:
        mvhd = _box(b"mvhd", b"\x01\0\0\0" + b"\0" * 16 + struct.pack(">IQ", 1000, int(seconds * 1000)) + b"\0" * 80)
    else:
        mvhd = _box(b"mvhd", b"\0" * 12 + struct.pack(">II", 1000, int(seconds * 1000)) + b"\0" * 80)
    hdlr = _box(b"hdlr", b"\0" * 8 + handler + b"\0" * 12 + b"Handler\0")
    trak = _box(b"trak", _box(b"mdia", hdlr))
    udta = _box(b"udta", _box(b"\xa9xyz", struct.pack(">HH", len(GPS), 0x15C7) + GPS)) if gps else b""
    return ftyp + _box(b"moov", mvhd + trak + udta) + _box(b"mdat", os.urandom(4000))


def _truncated():
    """moov written last (as many phones do) and cut off by an interrupted upload."""
    whole = mp4()
    moov_at = whole.index(b"moov") - 4
    mdat = whole[moov_at:]
    moov = mdat[:int.from_bytes(mdat[:4], "big")]
    return whole[:moov_at] + mdat[len(moov):] + moov[:-40]


def _el(eid, payload, unknown=False):
    size = b"\x01" + (b"\xff" * 7 if unknown else len(payload).to_bytes(7, "big"))
    return eid + size + payload


def webm(seconds=4.0, video=True, with_duration=True):
    header = _el(b"\x1a\x45\xdf\xa3", _el(b"\x42\x82", b"webm"))
    info = _el(b"\x2a\xd7\xb1", (1_000_000).to_bytes(3, "big"))
    if with_duration:
        info += _el(b"\x44\x89", struct.pack(">d", seconds * 1000))
    tracks = _el(b"\x16\x54\xae\x6b", _el(b"\xae", _el(b"\x83", b"\x01" if video else b"\x02")))
    cluster = _el(b"\x1f\x43\xb6\x75", os.urandom(3000))
    return header + _el(b"\x18\x53\x80\x67", _el(b"\x15\x49\xa9\x66", info) + tracks + cluster,
                        unknown=not with_duration)


def photo_bytes():
    return base64.b64decode(fresh_photo().split(",", 1)[1])


def upload(actor, blob):
    return actor.c.post("/api/media", content=blob, headers={**actor.h, "Content-Type": "application/octet-stream"})


# --------------------------------------------------------------------------- validation
def test_video_validation_is_done_on_the_server():
    clean, mime, ext, dur = media.parse_video(mp4(5))
    assert (mime, ext, dur) == ("video/mp4", "mp4", 5.0)
    assert GPS not in clean and len(clean) == len(mp4(5))          # location tag blanked, file size unchanged
    assert media.parse_video(mp4(7, brand=b"qt  "))[1:] == ("video/quicktime", "mov", 7.0)
    assert media.parse_video(mp4(12, mvhd_version=1))[3] == 12.0
    assert media.parse_video(webm(4.0))[1:] == ("video/webm", "webm", 4.0)
    assert media.parse_video(webm(with_duration=False))[3] is None  # live recordings may not declare a length
    for bad, code in ((mp4(45), 422), (webm(31.5), 422), (mp4(handler=b"soun"), 415), (webm(video=False), 415),
                      (b"\0\0\0\x18ftypxxxx" + os.urandom(3000), 415), (os.urandom(5000), 415),
                      (_truncated(), 422)):
        with pytest.raises(media.MediaError) as e:
            media.parse_video(bad)
        assert e.value.status == code


def test_upload_photos_and_videos(client, make_user, monkeypatch):
    c, off = make_user("citizen"), make_user("officer")
    p = upload(c, photo_bytes())
    assert p.status_code == 200, p.text
    assert p.json()["kind"] == "photo" and p.json()["check"]["verdict"] in ("passed", "needs_review")
    v = upload(c, mp4(6)).json()
    assert v["kind"] == "video" and v["duration_s"] == 6.0 and v["limits"]["max_files"] == 3
    w = upload(c, webm(with_duration=False)).json()
    assert w["duration_known"] is False
    assert upload(c, b"GIF89a" + os.urandom(500)).status_code == 415
    assert upload(c, mp4(40)).status_code == 422
    assert upload(c, b"").status_code == 422
    same = mp4(8)
    assert upload(c, same).status_code == 200 and upload(c, same).status_code == 422   # exact re-use refused
    assert upload(off, mp4(3)).status_code == 403
    assert client.post("/api/media", content=mp4(3)).status_code == 401
    monkeypatch.setattr(config, "MEDIA_MAX_VIDEO_BYTES", 3000)
    assert upload(c, mp4(3)).status_code == 413
    stored = query("SELECT stored_name FROM complaint_media WHERE kind='video'", one=True)["stored_name"]
    assert client.get("/evidence/media/" + stored).status_code == 404   # never served statically


def test_upload_request_size_guard(client, make_user):
    c = make_user("citizen")
    from routes import media as media_routes
    big = b"\0" * (media_routes.UPLOAD_LIMIT + 10)
    assert upload(c, big).status_code == 413
    # other routes keep the normal request limit
    r = c.c.post("/api/complaints", content=b"{" + b" " * (config.MAX_REQUEST_BYTES + 10) + b"}",
                 headers={**c.h, "Content-Type": "application/json"})
    assert r.status_code == 413


# --------------------------------------------------------------------------- attaching to a report
def test_report_with_three_attachments(client, make_user):
    owner, other, off = make_user("citizen"), make_user("citizen"), make_user("officer")
    ids = [upload(owner, photo_bytes()).json()["media_id"], upload(owner, mp4(5)).json()["media_id"],
           upload(owner, photo_bytes()).json()["media_id"]]
    r = file_complaint(owner, image=False, media_ids=ids)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["attachments"] == {"photos": 2, "videos": 1} and d["photo"] is not None
    cid = d["id"]
    mine = owner.get(f"/api/complaints/{cid}").json()
    assert mine["has_photo"] is True                       # first photo is the report's main photo
    assert [m["kind"] for m in mine["media"]] == ["photo", "video", "photo"]
    vid = mine["media"][1]
    got = owner.get(vid["url"])
    assert got.status_code == 200 and got.headers["content-type"] == "video/mp4" and GPS not in got.content
    assert "no-store" in got.headers["cache-control"]
    assert off.get(vid["url"]).status_code == 200
    assert other.get(vid["url"]).status_code == 404 and client.get(vid["url"]).status_code == 401
    inc_view = off.get(f"/api/complaints/{cid}").json()
    assert len(inc_view["media"]) == 3
    assert file_complaint(owner, image=False, media_ids=[ids[0]]).status_code == 409   # single use


def test_attachment_limits_and_ownership(client, make_user):
    owner, other = make_user("citizen"), make_user("citizen")
    ids = [upload(owner, mp4(3 + i)).json()["media_id"] for i in range(4)]
    assert file_complaint(owner, image=False, media_ids=ids).status_code == 422          # 4 files
    over = file_complaint(owner, media_ids=ids[:3])                                       # photo + 3 = 4
    assert over.status_code == 422 and "at most 3" in over.json()["detail"]["message"]
    assert file_complaint(other, image=False, media_ids=[ids[0]]).status_code == 409     # someone else's upload
    assert file_complaint(owner, image=False, media_ids=[ids[0], ids[0]]).status_code == 409
    assert file_complaint(owner, image=False, media_ids=["nope"]).status_code == 409
    assert query("SELECT COUNT(*) c FROM complaints", one=True)["c"] == 0
    assert query("SELECT COUNT(*) c FROM complaint_media WHERE complaint_id IS NOT NULL", one=True)["c"] == 0
    v = file_complaint(owner, image=False, media_ids=[ids[0]])                            # a video alone is enough
    assert v.status_code == 200 and v.json()["attachments"] == {"photos": 0, "videos": 1}
    assert owner.get(f"/api/complaints/{v.json()['id']}").json()["has_photo"] is False
    none = file_complaint(owner, image=False, text="Nothing attached to this pothole report at all.")
    assert none.status_code == 422 and "photo or video" in none.json()["detail"]["message"]


def test_remove_and_purge_unattached_uploads(client, make_user):
    owner, other = make_user("citizen"), make_user("citizen")
    a = upload(owner, mp4(4)).json()["media_id"]
    path = media.resolve(query("SELECT * FROM complaint_media WHERE token=?", (a,), one=True))
    assert path and os.path.isfile(path)
    assert other.c.delete(f"/api/media/{a}", headers=other.h).status_code == 404
    assert owner.c.delete(f"/api/media/{a}", headers=owner.h).status_code == 200
    assert not os.path.exists(path)
    b = upload(owner, photo_bytes()).json()["media_id"]
    file_complaint(owner, image=False, media_ids=[b])
    assert owner.c.delete(f"/api/media/{b}", headers=owner.h).status_code == 404       # attached: kept
    upload(owner, mp4(5))
    old = (utcnow() - timedelta(hours=config.MEDIA_UNLINKED_TTL_HOURS + 1)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        conn.execute("UPDATE complaint_media SET created_at=?", (old,))
    assert media.purge() == 1
    assert query("SELECT COUNT(*) c FROM complaint_media", one=True)["c"] == 1


def test_reused_attachment_cannot_be_resolution_proof(client, make_user):
    c, off = make_user("citizen"), make_user("officer")
    raw = photo_bytes()
    mid = upload(c, raw).json()["media_id"]
    cid = file_complaint(c, image=False, media_ids=[mid]).json()["id"]
    proof = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    r = off.post(f"/api/complaints/{cid}/resolve", json={"proof": proof, "notes": "done"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- address search
def test_address_search_offline_wards(client, make_user):
    c = make_user("citizen")
    ward = next(iter(config.WARDS))
    d = c.get("/api/geo/search", params={"q": ward}).json()
    assert d["provider"] is None and d["results"][0]["source"] == "ward_centre" and d["results"][0]["approximate"]
    none = c.get("/api/geo/search", params={"q": "zzzz unknown place"}).json()
    assert none["results"] == [] and "not enabled" in none["message"]
    assert c.get("/api/geo/search", params={"q": "ab"}).json()["results"] == []
    assert c.get("/api/geo/search", params={"q": "x" * 200}).status_code == 422
    assert client.get("/api/geo/search", params={"q": ward}).status_code == 401


def test_address_search_with_provider(client, make_user, monkeypatch):
    c = make_user("citizen")
    monkeypatch.setattr(config, "REVERSE_GEOCODER", "nominatim")
    monkeypatch.setattr(geocode.time, "sleep", lambda s: None)
    seen = []

    class R:
        status_code = 200

        def json(self):
            return [{"display_name": "Fergusson College Road, Shivajinagar, Pune", "lat": "18.5236", "lon": "73.8410"},
                    {"display_name": "Somewhere in Delhi", "lat": "28.61", "lon": "77.20"},
                    {"display_name": "Broken", "lat": "x", "lon": "y"}]

    def fake_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        seen.append((url, params, headers))
        return R()
    monkeypatch.setattr(geocode.requests, "get", fake_get)
    d = c.get("/api/geo/search", params={"q": "FC Road", "lang": "mr"}).json()
    assert [r["label"] for r in d["results"]] == ["Fergusson College Road, Shivajinagar, Pune"]
    assert d["results"][0]["source"] == "nominatim" and "OpenStreetMap" in d["attribution"]
    url, params, headers = seen[0]
    assert url.endswith("/search") and params["bounded"] == 1 and params["countrycodes"] == "in"
    assert params["viewbox"].count(",") == 3 and params["accept-language"] == "mr"
    assert headers["User-Agent"].startswith("CivicMind")
    c.get("/api/geo/search", params={"q": "fc  road", "lang": "mr"})
    assert len(seen) == 1                                   # cached
    codes = [c.get("/api/geo/search", params={"q": f"place {i}"}).status_code for i in range(8)]
    assert 429 in codes


def test_search_location_source_is_accepted(client, make_user):
    c = make_user("citizen")
    r = file_complaint(c, location_source="search", geocoded_address="FC Road, Pune")
    assert r.status_code == 200
    assert c.get(f"/api/complaints/{r.json()['id']}").json()["location"]["source"] == "search"
    assert file_complaint(c, location_source="search_adjusted", text="Pin moved after searching the address.").status_code == 200
