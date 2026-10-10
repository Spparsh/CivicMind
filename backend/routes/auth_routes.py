"""CivicMind — authentication routes.

Registration always creates a CITIZEN. Any `role` field a client sends is
ignored (the request model does not even accept it). Officers and admins are
created by an admin or by the local `manage.py` command.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import (authenticate, create_token, create_user, current_user, validate_email,
                  validate_password, check_login_allowed, record_login_failure,
                  clear_login_failures, client_ip, hash_password, verify_password)
from database import query, execute
from config import ROLE_HOME, WARDS, CIVILIAN_LANGUAGES

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class RegisterIn(BaseModel):
    model_config = {"extra": "ignore"}
    name: str = Field(min_length=2, max_length=80)
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    phone: str | None = Field(default=None, max_length=20)
    ward: str | None = Field(default=None, max_length=60)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class PreferencesIn(BaseModel):
    preferred_language: str = Field(max_length=5)


def _session(user):
    return {
        "token": create_token(user["id"], user["role"], user.get("token_version", 0)),
        "user": {k: user.get(k) for k in ("id", "name", "email", "role", "department", "ward",
                                          "preferred_language")},
        # Portal is chosen from the server-side role, never from client input.
        "home": ROLE_HOME.get(user["role"], "/"),
    }


@router.post("/login")
def login(body: LoginIn, request: Request):
    ip = client_ip(request)
    check_login_allowed(ip, body.email)
    user = authenticate(body.email, body.password)
    if not user:
        record_login_failure(ip, body.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    clear_login_failures(ip, body.email)
    return _session(user)


@router.post("/register")
def register(body: RegisterIn, request: Request):
    check_login_allowed(client_ip(request), "__register__")
    email = validate_email(body.email)
    validate_password(body.password)
    name = body.name.strip()
    if len(name) < 2:
        raise HTTPException(422, "Please enter your name.")
    ward = (body.ward or "").strip() or None
    if ward and WARDS and ward not in WARDS:
        raise HTTPException(422, "Unknown ward. Leave it blank or pick one from the list.")
    if query("SELECT id FROM users WHERE email=?", (email,), one=True):
        # Counts toward the throttle so the endpoint cannot be used to enumerate accounts quickly.
        record_login_failure(client_ip(request), "__register__")
        raise HTTPException(status_code=409, detail="That email is already registered. Sign in instead.")
    uid = create_user(name, email, body.password, role="citizen", phone=body.phone, ward=ward)
    user = query("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return _session(user)


@router.get("/me")
def me(user: dict = Depends(current_user)):
    return user | {"home": ROLE_HOME.get(user["role"], "/")}


@router.post("/preferences")
def preferences(body: PreferencesIn, user: dict = Depends(current_user)):
    if body.preferred_language not in CIVILIAN_LANGUAGES:
        raise HTTPException(422, {"message": "Unsupported language.", "allowed": list(CIVILIAN_LANGUAGES)})
    execute("UPDATE users SET preferred_language=? WHERE id=?", (body.preferred_language, user["id"]))
    return {"ok": True, "preferred_language": body.preferred_language}


@router.post("/logout-all")
def logout_all(user: dict = Depends(current_user)):
    """Invalidate every token issued to this user (all devices)."""
    execute("UPDATE users SET token_version = token_version + 1 WHERE id=?", (user["id"],))
    return {"ok": True}


@router.post("/password")
def change_password(body: PasswordIn, user: dict = Depends(current_user)):
    row = query("SELECT password_hash FROM users WHERE id=?", (user["id"],), one=True)
    if not row or not verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(403, "Current password is incorrect.")
    validate_password(body.new_password)
    execute("UPDATE users SET password_hash=?, token_version = token_version + 1 WHERE id=?",
            (hash_password(body.new_password), user["id"]))
    fresh = query("SELECT * FROM users WHERE id=?", (user["id"],), one=True)
    return _session(fresh)
