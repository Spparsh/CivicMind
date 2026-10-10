"""
CivicMind — central configuration.

Everything tunable is loaded here so NOTHING is hardcoded in the application
code:

  * domain data (city, categories, departments, wards, asset rules) lives in
    ../data/civicmind_config.json  — edit that file, not the code;
  * secrets & environment values come from a .env file or real environment
    variables (see .env.example).

Import order is deliberately dependency-free so this module can be imported
from anywhere without cycles.
"""
import json
import os
import secrets

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


# ---------------------------------------------------------------------------
# Minimal .env loader (no external dependency)
# ---------------------------------------------------------------------------
def _load_dotenv():
    if os.environ.get("CIVICMIND_SKIP_DOTENV") == "1":
        return
    for path in (os.path.join(PROJECT_ROOT, ".env"), os.path.join(BASE_DIR, ".env")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip().strip('"').strip("'")
                    # real environment variables win over the .env file
                    if key and val and key not in os.environ:
                        os.environ[key] = val
        except Exception:
            pass


_load_dotenv()


def _env(key, default=""):
    return os.environ.get(key, default)


def _env_bool(key, default=False):
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


CONFIG_FILE = _env("CIVICMIND_CONFIG", os.path.join(DATA_DIR, "civicmind_config.json"))
DB_PATH = os.path.abspath(_env("CIVICMIND_DB", os.path.join(BASE_DIR, "civicmind.db")))
# Legacy public upload folder (no longer served). Kept only so evidence written
# by older versions can still be found and served through the authorised API.
LEGACY_UPLOAD_DIR = os.path.join(PROJECT_ROOT, "uploads")
# Private evidence store — NOT mounted as static files. Served only via
# authorised API endpoints.
EVIDENCE_DIR = os.path.abspath(_env("CIVICMIND_EVIDENCE_DIR", os.path.join(PROJECT_ROOT, "private", "evidence")))
SECRET_FILE = os.path.join(BASE_DIR, ".secret_key")

os.makedirs(EVIDENCE_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Domain configuration (from data/civicmind_config.json)
# ---------------------------------------------------------------------------
_DEFAULTS = {
    "city_name": "Pune",
    "city_center": {"lat": 18.5204, "lng": 73.8567},
    "service_radius_km": 60,
    "categories": [],
    "departments": {},
    "wards": [],
    "demo_accounts": [],
    "require_issue_photo": False,
    "priority_thresholds": {"critical": 80, "high": 60, "medium": 40},
    "cluster": {"radius_km": 1.5, "window_hours": 48, "min_size": 3},
    "category_groups": [],
    "asset_types": {},
    "category_asset_types": {},
    "asset_match": {"radius_m": 150},
    "risk_rules": {},
}


def _load_config():
    data = dict(_DEFAULTS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            data.update(json.load(fh))
    except FileNotFoundError:
        print(f"[CivicMind] Config file not found at {CONFIG_FILE}; using defaults.")
    except Exception as exc:  # noqa: BLE001
        print(f"[CivicMind] Could not read config file: {exc}")
    return data


CFG = _load_config()

CITY_NAME = CFG.get("city_name", "Pune")
CITY_CENTER = CFG.get("city_center", _DEFAULTS["city_center"])
SERVICE_RADIUS_KM = float(CFG.get("service_radius_km", 60))
CATEGORIES = CFG.get("categories", [])
DEPARTMENT_MAP = CFG.get("departments", {})
WARDS_RAW = CFG.get("wards", [])
# Demo account *identities* (role / name / email). Passwords are never stored
# in config — the explicit demo seeding command generates them locally.
DEMO_ACCOUNTS = CFG.get("demo_accounts", [])
REQUIRE_ISSUE_PHOTO = bool(CFG.get("require_issue_photo", False))
CATEGORY_GROUPS = [set(g) for g in CFG.get("category_groups", [])]

# ward lookup: name -> (lat, lng, zone)
WARDS = {w["ward"]: (w["lat"], w["lng"], w.get("zone", "")) for w in WARDS_RAW}
ZONES = sorted({w.get("zone", "") for w in WARDS_RAW if w.get("zone")})

_thr = CFG.get("priority_thresholds", {})
PRIORITY_CRITICAL_THRESHOLD = float(_env("CIVICMIND_CRITICAL", _thr.get("critical", 80)))
PRIORITY_HIGH_THRESHOLD = float(_env("CIVICMIND_HIGH", _thr.get("high", 60)))
PRIORITY_MEDIUM_THRESHOLD = float(_env("CIVICMIND_MEDIUM", _thr.get("medium", 40)))

_cl = CFG.get("cluster", {})
CLUSTER_RADIUS_KM = float(_env("CIVICMIND_CLUSTER_RADIUS_KM", _cl.get("radius_km", 1.5)))
CLUSTER_TIME_WINDOW_HOURS = float(_env("CIVICMIND_CLUSTER_WINDOW_H", _cl.get("window_hours", 48)))
MIN_CLUSTER_SIZE = int(_env("CIVICMIND_MIN_CLUSTER", _cl.get("min_size", 3)))

# Infrastructure intelligence
ASSET_TYPES = CFG.get("asset_types", {})                    # type -> {label, department}
CATEGORY_ASSET_TYPES = CFG.get("category_asset_types", {})  # category -> [asset types]
ASSET_MATCH_RADIUS_M = float(CFG.get("asset_match", {}).get("radius_m", 150))
RISK_RULES = CFG.get("risk_rules", {})

# ---------------------------------------------------------------------------
# Runtime mode
# ---------------------------------------------------------------------------
# Demo mode is switched on explicitly (run_demo.* sets it). It no longer changes
# what any page shows: account e-mails and passwords are never served.
DEMO_MODE = _env_bool("CIVICMIND_DEMO_MODE", False)
# "production" makes start-up strict: a fixed CIVICMIND_SECRET is required (a generated
# key file would sign everyone out on every redeploy) and the demo database is refused.
ENVIRONMENT = _env("CIVICMIND_ENV", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT == "production"
# Comma-separated list of allowed cross-origin callers. Empty = same-origin only.
CORS_ORIGINS = [o.strip() for o in _env("CIVICMIND_CORS_ORIGINS", "").split(",") if o.strip()]
MAX_REQUEST_BYTES = int(_env("CIVICMIND_MAX_REQUEST_BYTES", str(9 * 1024 * 1024)))
MAX_IMAGE_BYTES = int(_env("CIVICMIND_MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))
MAX_IMAGE_PIXELS = int(_env("CIVICMIND_MAX_IMAGE_PIXELS", str(40_000_000)))


# ---------------------------------------------------------------------------
# Security — never ship a hardcoded secret.
# Priority: env var  ->  persisted .secret_key file  ->  freshly generated.
# ---------------------------------------------------------------------------
def _resolve_secret():
    env_secret = _env("CIVICMIND_SECRET").strip()
    if env_secret:
        return env_secret
    try:
        if os.path.isfile(SECRET_FILE):
            with open(SECRET_FILE, "r", encoding="utf-8") as fh:
                existing = fh.read().strip()
            if existing:
                return existing
        generated = secrets.token_urlsafe(48)
        with open(SECRET_FILE, "w", encoding="utf-8") as fh:
            fh.write(generated)
        try:
            os.chmod(SECRET_FILE, 0o600)
        except OSError:
            pass
        return generated
    except Exception:
        # last resort — still unique per process, just not persistent
        return secrets.token_urlsafe(48)


SECRET_KEY = _resolve_secret()
SECRET_FROM_ENV = bool(_env("CIVICMIND_SECRET").strip())


def production_problems():
    """Start-up checks for CIVICMIND_ENV=production. Returns a list of problems (empty = OK)."""
    if not IS_PRODUCTION:
        return []
    problems = []
    if not SECRET_FROM_ENV or len(_env("CIVICMIND_SECRET").strip()) < 32:
        problems.append("CIVICMIND_SECRET must be set to a random value of at least 32 characters.")
    if DEMO_MODE:
        problems.append("CIVICMIND_DEMO_MODE must not be enabled in production.")
    if os.path.normpath(DB_PATH).replace("\\", "/").endswith("data/demo/civicmind_demo.db"):
        problems.append("CIVICMIND_DB points at the demo database.")
    if _env_bool("TINYFISH_MOCK_MODE", False):
        problems.append("TINYFISH_MOCK_MODE must be false in production (mock content is fictional).")
    return problems
TOKEN_TTL_HOURS = int(_env("CIVICMIND_TOKEN_TTL_HOURS", "12"))
PBKDF2_ITERATIONS = int(_env("CIVICMIND_PBKDF2_ITERATIONS", "310000"))

# ---------------------------------------------------------------------------
# Sarvam AI
# ---------------------------------------------------------------------------
SARVAM_API_KEY = "" if _env_bool("CIVICMIND_DISABLE_AI") else _env("SARVAM_API_KEY").strip()
SARVAM_BASE_URL = _env("SARVAM_BASE_URL", "https://api.sarvam.ai")
SARVAM_CHAT_MODEL = _env("SARVAM_CHAT_MODEL", "sarvam-105b")
SARVAM_TIMEOUT = float(_env("SARVAM_TIMEOUT", "25"))
# Optional vision model for an *observation note* on photos (never a verdict).
SARVAM_VISION_MODEL = _env("SARVAM_VISION_MODEL", "").strip()


# ---------------------------------------------------------------------------
# TinyFish (public data-source discovery). Server-side only: the key is never
# sent to the browser, logged or returned by any endpoint.
# ---------------------------------------------------------------------------
def _env_int(key, default, lo, hi):
    try:
        val = int(_env(key, str(default)))
    except ValueError:
        val = default
    return max(lo, min(hi, val))


TINYFISH_API_KEY = _env("TINYFISH_API_KEY").strip()
TINYFISH_SEARCH_URL = _env("TINYFISH_SEARCH_URL", "https://api.search.tinyfish.ai").strip()
TINYFISH_FETCH_URL = _env("TINYFISH_FETCH_URL", "https://api.fetch.tinyfish.ai").strip()
TINYFISH_AGENT_URL = _env("TINYFISH_AGENT_URL", "https://agent.tinyfish.ai").strip()
TINYFISH_ENABLE_AGENT = _env_bool("TINYFISH_ENABLE_AGENT", False)
TINYFISH_MOCK_MODE = _env_bool("TINYFISH_MOCK_MODE", False)
TINYFISH_TIMEOUT_SECONDS = _env_int("TINYFISH_TIMEOUT_SECONDS", 30, 5, 150)
# The Fetch batch ceiling is 120 s on TinyFish's side; allow a little more locally.
TINYFISH_FETCH_TIMEOUT_SECONDS = _env_int("TINYFISH_FETCH_TIMEOUT_SECONDS", 150, 10, 180)
TINYFISH_MAX_FETCH_URLS = _env_int("TINYFISH_MAX_FETCH_URLS", 10, 1, 10)   # provider limit: 10
TINYFISH_MAX_SEARCH_RESULTS = _env_int("TINYFISH_MAX_SEARCH_RESULTS", 20, 1, 50)
TINYFISH_MAX_CONTENT_CHARS = _env_int("TINYFISH_MAX_CONTENT_CHARS", 200_000, 1_000, 1_000_000)
TINYFISH_CACHE_TTL_SECONDS = _env_int("TINYFISH_CACHE_TTL_SECONDS", 900, 0, 86_400)
TINYFISH_AGENT_MAX_DURATION_SECONDS = _env_int("TINYFISH_AGENT_MAX_DURATION_SECONDS", 120, 10, 600)
# max_steps is beta-gated by TinyFish (403 on non-beta accounts); only sent when set.
TINYFISH_AGENT_MAX_STEPS = _env_int("TINYFISH_AGENT_MAX_STEPS", 0, 0, 500)
TINYFISH_AGENT_DAILY_LIMIT = _env_int("TINYFISH_AGENT_DAILY_LIMIT", 5, 0, 200)
# Extra domains an admin may send the Agent to (besides domains of selected candidates).
TINYFISH_AGENT_ALLOWED_DOMAINS = [d.strip().lower().lstrip(".") for d in
                                  _env("TINYFISH_AGENT_ALLOWED_DOMAINS", "").split(",") if d.strip()]
# Resolve hostnames and refuse any that point at private/internal addresses.
TINYFISH_RESOLVE_DNS = _env_bool("TINYFISH_RESOLVE_DNS", True)

# Languages offered to civilians (the AI provider may support fewer — see services/assistant.py).
CIVILIAN_LANGUAGES = {"en": "English", "hi": "हिन्दी (Hindi)", "mr": "मराठी (Marathi)"}

# ---------------------------------------------------------------------------
# Voice descriptions (speech-to-text via Sarvam; audio kept private)
# ---------------------------------------------------------------------------
SARVAM_STT_MODEL = _env("SARVAM_STT_MODEL", "saaras:v4").strip()
SARVAM_STT_TIMEOUT = float(_env("SARVAM_STT_TIMEOUT", "30"))
VOICE_MAX_SECONDS = 15.0
VOICE_MAX_BYTES = _env_int("CIVICMIND_VOICE_MAX_BYTES", 1_200_000, 100_000, 5_000_000)
# Unlinked recordings are deleted after this many hours; linked audio after the
# complaint has been resolved for VOICE_RETENTION_DAYS (the transcript is kept).
VOICE_UNLINKED_TTL_HOURS = _env_int("CIVICMIND_VOICE_UNLINKED_TTL_HOURS", 24, 1, 168)
VOICE_RETENTION_DAYS = _env_int("CIVICMIND_VOICE_RETENTION_DAYS", 90, 1, 3650)

# ---------------------------------------------------------------------------
# Report attachments: up to MEDIA_MAX_FILES photos or short videos per report.
# Videos are uploaded as raw bytes (no base64) to a dedicated endpoint.
# ---------------------------------------------------------------------------
MEDIA_MAX_FILES = 3
MEDIA_MAX_VIDEO_BYTES = _env_int("CIVICMIND_MEDIA_MAX_VIDEO_BYTES", 30 * 1024 * 1024, 1_000_000, 100 * 1024 * 1024)
MEDIA_MAX_VIDEO_SECONDS = float(_env_int("CIVICMIND_MEDIA_MAX_VIDEO_SECONDS", 30, 5, 300))
MEDIA_UNLINKED_TTL_HOURS = VOICE_UNLINKED_TTL_HOURS

# ---------------------------------------------------------------------------
# Reverse geocoding for "Use my current location". OFF unless deliberately
# enabled. The public Nominatim service has a usage policy (max 1 request/s
# per application, identifying User-Agent, caching, attribution, explicit
# user action only): https://operations.osmfoundation.org/policies/nominatim/
# ---------------------------------------------------------------------------
REVERSE_GEOCODER = _env("CIVICMIND_REVERSE_GEOCODER", "none").strip().lower()   # none | nominatim
REVERSE_GEOCODER_URL = _env("CIVICMIND_REVERSE_GEOCODER_URL", "https://nominatim.openstreetmap.org/reverse").strip()
# Address search ("search an address" box) uses the same switch and provider.
GEOCODER_SEARCH_URL = _env("CIVICMIND_GEOCODER_SEARCH_URL", "https://nominatim.openstreetmap.org/search").strip()
REVERSE_GEOCODER_USER_AGENT = _env("CIVICMIND_REVERSE_GEOCODER_USER_AGENT",
                                   "CivicMind/0.5 (Build with Bharat 4.0 hackathon prototype)").strip()

# Role -> home dashboard
ROLE_HOME = {
    "citizen": "/citizen.html",
    "officer": "/dashboard.html",
    "admin": "/admin.html",
}
ROLES = ("citizen", "officer", "admin")
