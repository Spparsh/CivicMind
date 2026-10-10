"""CivicMind — admin routes. EVERY route here requires the admin role
(reads included), enforced by the router-level dependency."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import require_admin, create_user, validate_email, validate_password
from config import CATEGORIES, DEPARTMENT_MAP, ROLES, WARDS
from database import query, execute, is_demo_db
from services import bootstrap, evidence
from routes.common import staff_complaint, safe_json

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class UserIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    role: str = Field(default="citizen", max_length=10)
    department: str | None = Field(default=None, max_length=120)
    ward: str | None = Field(default=None, max_length=60)
    phone: str | None = Field(default=None, max_length=20)


class ResetIn(BaseModel):
    confirm: str = ""


@router.get("/users")
def users():
    return query("""SELECT id, name, email, role, department, ward, is_active, created_at
                    FROM users ORDER BY role, name""")


@router.post("/users")
def add_user(body: UserIn, admin: dict = Depends(require_admin)):
    email = validate_email(body.email)
    validate_password(body.password)
    if body.role not in ROLES:
        raise HTTPException(422, f"Role must be one of: {', '.join(ROLES)}")
    if body.department and body.department not in {r["name"] for r in query(
            "SELECT name FROM departments WHERE is_active=1")}:
        raise HTTPException(422, "Unknown department")
    if body.ward and WARDS and body.ward not in WARDS:
        raise HTTPException(422, "Unknown ward")
    if query("SELECT id FROM users WHERE email=?", (email,), one=True):
        raise HTTPException(409, "Email already exists")
    uid = create_user(body.name, email, body.password, body.role, body.phone, body.department, body.ward)
    execute("INSERT INTO activity (actor, actor_id, action, detail) VALUES (?,?,?,?)",
            (admin["name"], admin["id"], "user_created", f"Created {body.role} account #{uid}"))
    return {"id": uid, "ok": True}


@router.get("/config")
def config():
    return {"categories": CATEGORIES,
            "departments": [r["name"] for r in query("SELECT name FROM departments WHERE is_active=1 ORDER BY name")],
            "roles": list(ROLES), "wards": sorted(WARDS), "read_only": True,
            "note": "Edit data/civicmind_config.json and restart to change configuration."}


@router.get("/stats")
def stats():
    total = query("SELECT COUNT(*) c FROM complaints WHERE status != 'rejected'", one=True)["c"]
    resolved = query("SELECT COUNT(*) c FROM complaints WHERE status='resolved'", one=True)["c"]
    c = lambda sql: query(sql, one=True)["c"]  # noqa: E731
    return {
        "users": c("SELECT COUNT(*) c FROM users"),
        "complaints": total,
        "open_complaints": c("SELECT COUNT(*) c FROM complaints WHERE status NOT IN ('resolved','rejected')"),
        "resolved_complaints": resolved,
        "incidents": c("SELECT COUNT(*) c FROM incidents"),
        "actions": c("SELECT COUNT(*) c FROM actions"),
        "resolutions": c("SELECT COUNT(*) c FROM resolutions"),
        "assets": c("SELECT COUNT(*) c FROM assets"),
        "open_findings": c("SELECT COUNT(*) c FROM risk_findings WHERE status IN ('open','acknowledged')"),
        "resolution_rate": round(resolved / total * 100, 1) if total else None,
        "demo_database": is_demo_db(),
    }


@router.get("/complaints")
def all_complaints(admin: dict = Depends(require_admin)):
    rows = query("""
        SELECT c.*, u.name AS citizen_name, u.email AS citizen_email, o.name AS officer_name,
               i.public_id AS incident_public_id, i.title AS incident_title, i.status AS incident_status
        FROM complaints c LEFT JOIN users u ON u.id = c.user_id LEFT JOIN users o ON o.id = c.resolved_by
        LEFT JOIN incidents i ON i.id = c.incident_id
        ORDER BY c.created_at DESC, c.id DESC LIMIT 500""")
    out = []
    for r in rows:
        inc = {"id": r["incident_id"], "public_id": r["incident_public_id"], "title": r["incident_title"],
               "status": r["incident_status"]} if r["incident_id"] else None
        out.append(staff_complaint(r, admin, inc, None, r.get("officer_name")))
    return out


@router.get("/resolutions")
def all_resolutions():
    incident_res = query("""
        SELECT r.id, r.action_id, 'incident' AS scope, i.public_id AS ref, i.id AS incident_id, i.title,
               i.category, i.ward, r.before_metric, r.after_metric, r.summary, r.status, r.verified_at,
               r.verification_type, r.proof_check, r.proof_path IS NOT NULL AS has_proof, u.name AS officer_name
        FROM resolutions r JOIN incidents i ON i.id = r.incident_id LEFT JOIN users u ON u.id = r.resolved_by
        ORDER BY r.verified_at DESC LIMIT 300""")
    complaint_res = query("""
        SELECT c.id, NULL AS action_id, 'complaint' AS scope, c.public_id AS ref, NULL AS incident_id,
               c.category AS title, c.category, c.ward, NULL AS before_metric, NULL AS after_metric,
               c.resolution_notes AS summary, 'verified' AS status, c.resolved_at AS verified_at,
               'single_complaint' AS verification_type, c.resolution_proof_check AS proof_check,
               c.resolution_proof_path IS NOT NULL AS has_proof, u.name AS officer_name
        FROM complaints c LEFT JOIN users u ON u.id = c.resolved_by
        WHERE c.resolution_proof_path IS NOT NULL ORDER BY c.resolved_at DESC LIMIT 300""")
    rows = incident_res + complaint_res
    for r in rows:
        r["proof_check"] = evidence.public_check(safe_json(r.get("proof_check")))
    rows.sort(key=lambda r: r.get("verified_at") or "", reverse=True)
    return rows


class UserUpdateIn(BaseModel):
    role: str | None = Field(default=None, max_length=10)
    department: str | None = Field(default=None, max_length=120)
    ward: str | None = Field(default=None, max_length=60)
    is_active: bool | None = None


@router.post("/users/{user_id}")
def update_user(user_id: int, body: UserUpdateIn, admin: dict = Depends(require_admin)):
    """Change role, department, ward or active state. Role/active changes revoke the user's existing tokens."""
    target = query("SELECT id, name, role, is_active FROM users WHERE id=?", (user_id,), one=True)
    if not target:
        raise HTTPException(404, "User not found")
    if body.role is not None and body.role not in ROLES:
        raise HTTPException(422, f"Role must be one of: {', '.join(ROLES)}")
    if user_id == admin["id"] and ((body.role and body.role != "admin") or body.is_active is False):
        raise HTTPException(409, "You cannot remove your own admin access.")
    active_depts = {r["name"] for r in query("SELECT name FROM departments WHERE is_active=1")}
    if body.department and body.department not in active_depts:
        raise HTTPException(422, "Unknown or inactive department")
    if body.ward and WARDS and body.ward not in WARDS:
        raise HTTPException(422, "Unknown ward")
    sets, params, changes = [], [], []
    for col in ("role", "department", "ward"):
        val = getattr(body, col)
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val or None)
            changes.append(f"{col}={val or '—'}")
    if body.is_active is not None:
        sets.append("is_active=?")
        params.append(int(body.is_active))
        changes.append(f"active={body.is_active}")
    if not sets:
        raise HTTPException(422, "Nothing to change")
    if body.role is not None or body.is_active is not None:
        sets.append("token_version = token_version + 1")
    execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*params, user_id))
    execute("INSERT INTO activity (actor, actor_id, action, detail) VALUES (?,?,?,?)",
            (admin["name"], admin["id"], "user_updated", f"User #{user_id}: {', '.join(changes)}"))
    return query("SELECT id, name, email, role, department, ward, is_active FROM users WHERE id=?", (user_id,), one=True)


@router.get("/departments")
def departments():
    rows = query("SELECT * FROM departments ORDER BY is_active DESC, name")
    for r in rows:
        r["officers"] = query("SELECT COUNT(*) c FROM users WHERE department=? AND role IN ('officer','admin')",
                              (r["name"],), one=True)["c"]
        r["in_config"] = r["name"] in set(DEPARTMENT_MAP.values())
    return rows


class DepartmentIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    description: str | None = Field(default=None, max_length=300)
    contact_role: str | None = Field(default=None, max_length=120)


@router.post("/departments")
def add_department(body: DepartmentIn, admin: dict = Depends(require_admin)):
    name = " ".join(body.name.split())
    if query("SELECT 1 FROM departments WHERE lower(name)=lower(?)", (name,), one=True):
        raise HTTPException(409, "A department with that name exists")
    execute("INSERT INTO departments (name, description, contact_role) VALUES (?,?,?)",
            (name, (body.description or "").strip() or None, (body.contact_role or "").strip() or None))
    execute("INSERT INTO activity (actor, actor_id, action, detail) VALUES (?,?,?,?)",
            (admin["name"], admin["id"], "department_created", f"Department '{name}' created"))
    return {"ok": True}


class DepartmentStateIn(BaseModel):
    is_active: bool


@router.post("/departments/{dept_id}/state")
def department_state(dept_id: int, body: DepartmentStateIn, admin: dict = Depends(require_admin)):
    d = query("SELECT name FROM departments WHERE id=?", (dept_id,), one=True)
    if not d:
        raise HTTPException(404, "Department not found")
    if not body.is_active and d["name"] in set(DEPARTMENT_MAP.values()):
        raise HTTPException(409, "This department is used for complaint routing in the configuration; it cannot be "
                                 "deactivated here.")
    execute("UPDATE departments SET is_active=? WHERE id=?", (int(body.is_active), dept_id))
    execute("INSERT INTO activity (actor, actor_id, action, detail) VALUES (?,?,?,?)",
            (admin["name"], admin["id"], "department_updated",
             f"Department '{d['name']}' {'activated' if body.is_active else 'deactivated'}"))
    return {"ok": True}


@router.get("/analytics")
def analytics():
    """Citywide figures computed from records at request time (nothing is stored or estimated)."""
    return {
        "complaints_by_category": query("""SELECT category, COUNT(*) c, SUM(status='resolved') resolved
                                           FROM complaints GROUP BY category ORDER BY c DESC"""),
        "complaints_by_ward": query("""SELECT COALESCE(ward,'Unmapped') ward, COUNT(*) c,
                                       SUM(status NOT IN ('resolved','rejected')) open FROM complaints
                                       GROUP BY ward ORDER BY c DESC LIMIT 20"""),
        "work_orders_by_status": query("SELECT status, COUNT(*) c FROM work_orders GROUP BY status"),
        "repeat_assets": query("""SELECT a.asset_id, a.name, COUNT(DISTINCT iua.incident_id) incidents
                                  FROM incident_utility_assets iua JOIN utility_assets a ON a.id=iua.asset_id
                                  WHERE iua.status != 'rejected' GROUP BY a.id HAVING incidents >= 2
                                  ORDER BY incidents DESC LIMIT 10"""),
        "definition": "Counts of current records; repeat assets = utility assets linked to 2+ incidents.",
    }


@router.get("/activity")
def activity(limit: int = 100):
    """Audit trail: who did what, newest first (no secrets are ever written to it)."""
    limit = max(1, min(500, limit))
    return query("""SELECT a.id, a.actor, a.action, a.detail, a.incident_id, a.created_at, i.public_id AS incident_public_id
                    FROM activity a LEFT JOIN incidents i ON i.id = a.incident_id
                    ORDER BY a.id DESC LIMIT ?""", (limit,))


@router.post("/reset")
def reset(body: ResetIn, admin: dict = Depends(require_admin)):
    """Clear civic data on the ISOLATED DEMO DATABASE only (users and assets kept)."""
    if body.confirm != "RESET":
        raise HTTPException(422, "Type RESET to confirm.")
    try:
        return bootstrap.reset_data()
    except PermissionError as e:
        raise HTTPException(403, str(e))
