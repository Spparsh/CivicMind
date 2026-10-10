"""
CivicMind — local administration commands (run on the server, not over HTTP).

    python manage.py create-user --email a@b.in --name "Ward Officer" --role officer
    python manage.py set-password --email a@b.in
    python manage.py list-users

The password is prompted for (not echoed) unless --password is given.
Uses the database in CIVICMIND_DB (default backend/civicmind.db).
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
from database import init_db, query, execute  # noqa: E402
from auth import create_user, hash_password, MIN_PASSWORD_LEN, EMAIL_RE  # noqa: E402


def _password(args):
    pw = args.password or getpass.getpass("Password: ")
    if len(pw) < MIN_PASSWORD_LEN:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    return pw


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("--email", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--role", choices=config.ROLES, default="officer")
    c.add_argument("--department")
    c.add_argument("--password")
    s = sub.add_parser("set-password")
    s.add_argument("--email", required=True)
    s.add_argument("--password")
    sub.add_parser("list-users")
    args = ap.parse_args()

    init_db()
    print(f"Database: {config.DB_PATH}")
    if args.cmd == "create-user":
        email = args.email.strip().lower()
        if not EMAIL_RE.match(email):
            sys.exit("Invalid e-mail address.")
        if query("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            sys.exit("That e-mail already exists.")
        uid = create_user(args.name, email, _password(args), role=args.role, department=args.department)
        print(f"Created {args.role} #{uid} {email}")
    elif args.cmd == "set-password":
        email = args.email.strip().lower()
        if not query("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            sys.exit("No such user.")
        execute("UPDATE users SET password_hash=?, token_version=token_version+1 WHERE email=?",
                (hash_password(_password(args)), email))
        print("Password updated; existing sessions for this user were signed out.")
    else:
        for u in query("SELECT id, role, email, name FROM users ORDER BY role, email"):
            print(f"{u['id']:>4}  {u['role']:8} {u['email']:32} {u['name']}")


if __name__ == "__main__":
    main()
