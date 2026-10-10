"""Shared fixtures. Every test gets a fresh SQLite file and evidence directory;
the real database, .env and Sarvam API are never touched."""
import base64
import io
import os
import random
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="civicmind-tests-")
os.environ.update({
    "CIVICMIND_SKIP_DOTENV": "1",
    "CIVICMIND_DISABLE_AI": "1",
    "CIVICMIND_PBKDF2_ITERATIONS": "1000",
    "CIVICMIND_SECRET": "test-secret-not-for-production",
    "CIVICMIND_DB": os.path.join(_TMP, "boot.db"),
    "CIVICMIND_EVIDENCE_DIR": os.path.join(_TMP, "evidence-boot"),
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import auth  # noqa: E402
import config  # noqa: E402
import main  # noqa: E402
from services import evidence  # noqa: E402

PUNE = (18.5204, 73.8567)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    ev = tmp_path / "evidence"
    ev.mkdir()
    monkeypatch.setattr(config, "EVIDENCE_DIR", str(ev))
    monkeypatch.setattr(evidence, "EVIDENCE_DIR", str(ev))
    auth.reset_throttle()
    with TestClient(main.app) as c:
        yield c


def image_data_url(seed=1, size=(640, 480), fmt="JPEG", flat=False):
    rnd = random.Random(seed)
    im = Image.new("RGB", size, (120, 120, 120))
    if not flat:
        px = im.load()
        for y in range(0, size[1], 2):
            for x in range(0, size[0], 2):
                v = rnd.randint(0, 255)
                px[x, y] = (v, (v * 3) % 256, (v * 7) % 256)
    buf = io.BytesIO()
    im.save(buf, format=fmt)
    mime = {"JPEG": "jpeg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "BMP": "bmp"}[fmt]
    return f"data:image/{mime};base64," + base64.b64encode(buf.getvalue()).decode()


class Actor:
    def __init__(self, client, token, user):
        self.c, self.token, self.user = client, token, user
        self.h = {"Authorization": f"Bearer {token}"}

    def get(self, path, **kw):
        return self.c.get(path, headers=self.h, **kw)

    def post(self, path, json=None, **kw):
        return self.c.post(path, headers=self.h, json=json if json is not None else {}, **kw)


def _login(client, email, password):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    d = r.json()
    return Actor(client, d["token"], d["user"])


@pytest.fixture()
def make_user(client):
    counter = {"n": 0}

    def _make(role="citizen", name=None):
        counter["n"] += 1
        email = f"{role}{counter['n']}@example.org"
        if role == "citizen":
            r = client.post("/api/auth/register", json={"name": name or f"Citizen {counter['n']}",
                                                         "email": email, "password": "password123"})
            assert r.status_code == 200, r.text
        else:
            auth.create_user(name or f"{role.title()} {counter['n']}", email, "password123", role=role)
        return _login(client, email, "password123")
    return _make


_photo_seed = [50_000]


def fresh_photo():
    """A unique, valid issue photo (a photo is mandatory for every complaint)."""
    _photo_seed[0] += 1
    return image_data_url(_photo_seed[0], size=(320, 240))


def file_complaint(actor, category="Pothole", lat=None, lng=None, text=None, image=None, **extra):
    lat = PUNE[0] if lat is None else lat
    lng = PUNE[1] if lng is None else lng
    body = {"category": category, "latitude": lat, "longitude": lng,
            "description": text or f"There is a {category.lower()} problem here, please fix it soon.",
            "image": image if image is not None else fresh_photo(), **extra}
    if body["image"] is False:  # explicit "no photo" for policy tests
        del body["image"]
    return actor.post("/api/complaints", json=body)


def offset(lat, lng, dlat_m=0, dlng_m=0):
    return lat + dlat_m / 111_320, lng + dlng_m / 105_000
