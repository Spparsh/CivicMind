import base64
import io
import os

from PIL import Image

import config
from services import evidence
from conftest import file_complaint, image_data_url


def test_valid_image_is_reencoded_without_exif(client, make_user):
    im = Image.new("RGB", (640, 480))
    px = im.load()
    for y in range(480):
        for x in range(640):
            px[x, y] = ((x * 7) % 256, (y * 5) % 256, (x + y) % 256)
    exif = Image.Exif()
    exif[0x8825] = {2: (18, 31, 12)}  # GPS IFD
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif)
    assert b"Exif" in buf.getvalue()
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    r = file_complaint(make_user("citizen"), image=url)
    assert r.status_code == 200, r.text
    stored = os.listdir(config.EVIDENCE_DIR)
    assert len(stored) == 1 and stored[0].startswith("issue-") and stored[0].endswith(".jpg")
    with Image.open(os.path.join(config.EVIDENCE_DIR, stored[0])) as saved:
        assert not saved.getexif()


def test_png_and_webp_accepted(client, make_user):
    c = make_user("citizen")
    assert file_complaint(c, image=image_data_url(2, fmt="PNG")).status_code == 200
    assert file_complaint(c, image=image_data_url(3, fmt="WEBP"), text="Second pothole description here").status_code == 200


def test_unsupported_and_spoofed_files_rejected(client, make_user):
    c = make_user("citizen")
    gif = image_data_url(4, fmt="GIF")
    assert file_complaint(c, image=gif).status_code == 415
    # claims to be JPEG but is a GIF inside
    spoof = "data:image/jpeg;base64," + gif.split(",", 1)[1]
    assert file_complaint(c, image=spoof).status_code == 415
    html = "data:image/png;base64," + base64.b64encode(b"<script>alert(1)</script>" * 20).decode()
    assert file_complaint(c, image=html).status_code == 400
    assert file_complaint(c, image="data:image/png;base64,@@@not-base64@@@").status_code == 400
    assert file_complaint(c, image="/etc/passwd").status_code == 415
    assert file_complaint(c, image="../../backend/civicmind.db").status_code == 415
    assert os.listdir(config.EVIDENCE_DIR) == []


def test_oversized_image_rejected(client, make_user, monkeypatch):
    monkeypatch.setattr(evidence, "MAX_IMAGE_BYTES", 20_000)
    r = file_complaint(make_user("citizen"), image=image_data_url(5, size=(800, 800)))
    assert r.status_code == 413


def test_pixel_bomb_rejected(client, make_user, monkeypatch):
    monkeypatch.setattr(evidence, "MAX_IMAGE_PIXELS", 10_000)
    r = file_complaint(make_user("citizen"), image=image_data_url(6, size=(400, 400)))
    assert r.status_code == 413


def test_blank_and_reused_images_rejected(client, make_user):
    c = make_user("citizen")
    r = file_complaint(c, image=image_data_url(7, flat=True))
    assert r.status_code == 422 and "blank" in str(r.json()["detail"]["reasons"]).lower()
    assert file_complaint(c, image=image_data_url(8)).status_code == 200
    r = file_complaint(make_user("citizen"), image=image_data_url(8), text="Another pothole report elsewhere")
    assert r.status_code == 422 and "already submitted" in str(r.json()["detail"]["reasons"])
    assert len(os.listdir(config.EVIDENCE_DIR)) == 1  # rejected uploads leave no file behind


def test_resolve_path_blocks_traversal(tmp_path):
    for bad in ("../../etc/passwd", "..\\..\\x.jpg", "/uploads/../../main.py", "issue-zz.jpg", "", None,
                "issue-" + "0" * 32 + ".jpg/../../x"):
        assert evidence.resolve_path(bad) is None


def test_missing_file_returns_404(client, make_user):
    c = make_user("citizen")
    r = file_complaint(c, image=image_data_url(9)).json()
    for f in os.listdir(config.EVIDENCE_DIR):
        os.remove(os.path.join(config.EVIDENCE_DIR, f))
    assert c.get(f"/api/complaints/{r['id']}/photo").status_code == 404
    assert c.get(f"/api/complaints/{r['id']}/resolution-photo").status_code == 404


def test_check_result_is_honestly_scoped(client, make_user):
    r = file_complaint(make_user("citizen"), image=image_data_url(10)).json()
    assert r["photo"]["engine"] == "automated_checks"
    assert "not an independent field inspection" in r["photo"]["note"].lower()
