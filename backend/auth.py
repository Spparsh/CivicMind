"""
CivicMind — authentication and authorisation.

* Passwords: PBKDF2-HMAC-SHA256 with a per-user random salt. The iteration
  count is stored in each hash, so older (weaker) hashes keep working and are
  transparently upgraded on the next successful login.
* Tokens: compact HMAC-SHA256 signed tokens (no external JWT dependency).
  Claims: uid, role, tv (token version), iat, exp, typ. The role in the token
  is informational only — every request re-reads the user's CURRENT role from
  the database, so a demoted user loses access immediately.
* Revocation: bumping users.token_version (``POST /api/auth/logout-all``,
  password change) invalidates every token issued before it. A plain logout
  only discards the token client-side; until it expires it remains valid.
* Authorisation: use the FastAPI dependencies at the bottom of this module
  (`require_officer`, `require_admin`, ...). Never trust a role sent by the
  client.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time

from fastapi import Depends, Header, HTTPException, Request

from config import SECRET_KEY, TOKEN_TTL_HOURS, PBKDF2_ITERATIONS, ROLES
from database import query, execute

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
MIN_PASSWORD_LEN = 8


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(password: str, iterations: int = None) -> str:
    iterations = iterations or PBKDF2_ITERATIONS
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _needs_rehash(stored: str) -> bool:
    try:
        return int(stored.split("$")[1]) < PBKDF2_ITERATIONS
    except Exception:
        return True


# A fixed dummy hash so unknown e-mails cost the same time as wrong passwords.
_DUMMY_HASH = hash_password("timing-equaliser", iterations=PBKDF2_ITERATIONS)


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid e-mail address.")
    return email


def validate_password(password: str):
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(422, f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    if len(password) > 256:
        raise HTTPException(422, "Password is too long.")


# ---------------------------------------------------------------------------
# Tokens  (payload.signature, base64url)
# ---------------------------------------------------------------------------
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _sign(payload: str) -> str:
    return _b64(hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).digest())


def create_token(user_id: int, role: str, token_version: int = 0, ttl_hours: float = None) -> str:
    now = int(time.time())
    payload = json.dumps({
        "uid": int(user_id), "role": role, "tv": int(token_version or 0),
        "iat": now, "exp": now + int((ttl_hours or TOKEN_TTL_HOURS) * 3600), "typ": "access",
    }, separators=(",", ":"))
    body = _b64(payload.encode())
    return f"{body}.{_sign(body)}"


def decode_token(token: str):
    """Return the claims dict for a valid, unexpired token, else None."""
    try:
        if not token or len(token) > 2048:
            return None
        body, sig = token.split(".")
        if not hmac.compare_digest(_sign(body), sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(data, dict) or data.get("typ") != "access":
            return None
        if not isinstance(data.get("uid"), int) or not isinstance(data.get("exp"), int):
            return None
        if data["exp"] < time.time():
            return None
        return data
    except Exception:
        return None


def user_from_token(token: str):
    """Resolve a token to the CURRENT user row (or None)."""
    data = decode_token(token)
    if not data:
        return None
    user = query("""SELECT id, name, email, role, department, ward, preferred_language, token_version, is_active
                    FROM users WHERE id=?""", (data["uid"],), one=True)
    if not user or not user.get("is_active", 1):
        return None
    if int(user.get("token_version") or 0) != int(data.get("tv", 0)):
        return None
    user.pop("token_version", None)
    user.pop("is_active", None)
    return user


# ---------------------------------------------------------------------------
# Login throttling (in-process; good enough for a single-instance deployment)
# ---------------------------------------------------------------------------
_FAIL_WINDOW_S = 600
_MAX_FAILS = 5
_fails = {}
_fails_lock = threading.Lock()


def _throttle_key(ip, email):
    return f"{ip}|{(email or '').lower().strip()}"


def check_login_allowed(ip, email):
    key = _throttle_key(ip, email)
    with _fails_lock:
        now = time.time()
        attempts = [t for t in _fails.get(key, []) if now - t < _FAIL_WINDOW_S]
        _fails[key] = attempts
        if len(attempts) >= _MAX_FAILS:
            retry = int(_FAIL_WINDOW_S - (now - attempts[0])) + 1
            raise HTTPException(429, "Too many failed sign-in attempts. Try again later.",
                                headers={"Retry-After": str(retry)})


def record_login_failure(ip, email):
    with _fails_lock:
        _fails.setdefault(_throttle_key(ip, email), []).append(time.time())


def clear_login_failures(ip, email):
    with _fails_lock:
        _fails.pop(_throttle_key(ip, email), None)


def reset_throttle():
    with _fails_lock:
        _fails.clear()


def authenticate(email: str, password: str):
    user = query("SELECT * FROM users WHERE email=?", ((email or "").lower().strip(),), one=True)
    if not user:
        verify_password(password or "", _DUMMY_HASH)  # equalise timing
        return None
    if not user.get("is_active", 1) or not verify_password(password or "", user["password_hash"]):
        return None
    if _needs_rehash(user["password_hash"]):
        execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user["id"]))
    return user


def create_user(name, email, password, role="citizen", phone=None, department=None, ward=None):
    if role not in ROLES:
        raise ValueError("invalid role")
    return execute(
        """INSERT INTO users (name, email, password_hash, role, phone, department, ward)
           VALUES (?,?,?,?,?,?,?)""",
        (name.strip(), email.lower().strip(), hash_password(password), role,
         phone, department, ward),
    )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
def current_user(authorization: str = Header(default="")) -> dict:
    """Resolve the caller from the Bearer token."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in required")
    user = user_from_token(authorization[7:].strip())
    if not user:
        raise HTTPException(status_code=401, detail="Your session has expired. Please sign in again.")
    return user


def require_roles(*roles):
    def _dep(user: dict = Depends(current_user)) -> dict:
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="You do not have permission to do this.")
        return user
    return _dep


require_citizen = require_roles("citizen")
require_officer = require_roles("officer", "admin")
require_admin = require_roles("admin")


def is_staff(user) -> bool:
    return bool(user) and user.get("role") in ("officer", "admin")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
