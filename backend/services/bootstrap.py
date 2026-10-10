"""
CivicMind — startup bootstrap.

Startup NEVER creates accounts with known passwords. Accounts come from:
  * citizen self-registration;
  * an admin creating officers/admins in the admin console;
  * `python manage.py create-user ...` (local, explicit);
  * CIVICMIND_ADMIN_EMAIL + CIVICMIND_ADMIN_PASSWORD env vars (creates that
    admin once if missing — handy on a host);
  * `python seed_demo.py` for the ISOLATED demo database only.

`reset_data()` is refused unless the database is marked as a demo database.
"""
import os

from database import query, db, is_demo_db
from auth import create_user, verify_password

# Passwords shipped by earlier versions of this project. If an account still
# uses one, warn loudly — anyone who read the old README can sign in.
_LEGACY_DEFAULTS = {"citizen@civicmind.in": "citizen123", "officer@civicmind.in": "officer123",
                    "admin@civicmind.in": "admin123"}


def bootstrap():
    created = []
    email = os.environ.get("CIVICMIND_ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("CIVICMIND_ADMIN_PASSWORD", "")
    if email and password:
        if len(password) < 12:
            print("[CivicMind] CIVICMIND_ADMIN_PASSWORD must be at least 12 characters; admin not created.")
        elif not query("SELECT id FROM users WHERE email=?", (email,), one=True):
            create_user("Administrator", email, password, role="admin")
            created.append(email)
    weak = []
    for em, pw in _LEGACY_DEFAULTS.items():
        row = query("SELECT password_hash FROM users WHERE email=?", (em,), one=True)
        if row and verify_password(pw, row["password_hash"]):
            weak.append(em)
    if weak:
        print("[CivicMind] WARNING: these accounts still use the old published default passwords: "
              + ", ".join(weak) + ". Change them (python manage.py set-password) before any public use.")
    return {"created_accounts": created, "accounts_with_default_passwords": len(weak)}


def reset_data():
    """Delete all civic data, keep user accounts. DEMO DATABASE ONLY."""
    if not is_demo_db():
        raise PermissionError("Reset is only allowed on the isolated demo database.")
    with db(immediate=True) as conn:
        # The asset registry and its imported history are kept; only records
        # produced by the complaint workflow are removed.
        conn.execute("DELETE FROM asset_maintenance WHERE action_id IS NOT NULL")
        for t in ["risk_findings", "utility_findings", "incident_utility_assets", "complaint_assets",
                  "notifications", "activity",
                  "resolutions", "evidence", "actions", "incident_complaints"]:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("UPDATE complaints SET incident_id=NULL")
        conn.execute("DELETE FROM complaints")
        conn.execute("DELETE FROM incidents")
    return {"reset": True}
