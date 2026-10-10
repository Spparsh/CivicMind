"""
CivicMind — build the ISOLATED demo database (explicit command, never automatic).

    cd backend
    python seed_demo.py                 # creates/updates data/demo/civicmind_demo.db
    python seed_demo.py --reset         # wipe and rebuild the DEMO database only

Everything created here is FICTIONAL and labelled synthetic (is_synthetic=1,
data_source "Synthetic demo dataset (fictional)"). It is not municipal data.

Safety:
  * refuses to touch a database that is not marked as a demo database
    (e.g. your real backend/civicmind.db);
  * idempotent — re-running does not duplicate accounts, assets or reports;
  * demo passwords are generated locally (or taken from CIVICMIND_DEMO_PASSWORD)
    and written to data/demo/demo_credentials.txt — never served by the API.
"""
import argparse
import os
import random
import secrets
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEMO_DIR = os.path.join(ROOT, "data", "demo")
DEFAULT_DB = os.path.join(DEMO_DIR, "civicmind_demo.db")
SOURCE = "Synthetic demo dataset (fictional)"


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


def _photo_data_url(seed, path=None):
    """Deterministic synthetic 'photo' (noise + gradient) that passes the
    quality checks. Clearly not a real photograph."""
    import base64
    import io
    from PIL import Image
    rnd = random.Random(seed)
    w, h = 640, 480
    im = Image.new("RGB", (w, h))
    px = im.load()
    base = (rnd.randint(40, 120), rnd.randint(40, 120), rnd.randint(40, 120))
    for y in range(h):
        for x in range(0, w):
            n = rnd.randint(-40, 40)
            px[x, y] = (max(0, min(255, base[0] + x // 5 + n)), max(0, min(255, base[1] + y // 4 + n)),
                        max(0, min(255, base[2] + n)))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    if path:
        with open(path, "wb") as fh:
            fh.write(buf.getvalue())
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


ASSETS = [
    {"asset_code": "SL-DEMO-014", "asset_type": "streetlight", "name": "Streetlight at school crossing (fictional)",
     "latitude": 18.50620, "longitude": 73.80810, "criticality": "critical",
     "criticality_reason": "pedestrian crossing used by school children",
     "inspection_interval_days": 180, "last_inspected_at": _ago(days=40)},
    {"asset_code": "SL-DEMO-015", "asset_type": "streetlight", "name": "Streetlight, lane 3 (fictional)",
     "latitude": 18.50700, "longitude": 73.80990, "criticality": "medium",
     "inspection_interval_days": 180, "last_inspected_at": _ago(days=60)},
    {"asset_code": "RD-DEMO-007", "asset_type": "road_segment", "name": "Bus-stop road stretch (fictional, reference point)",
     "latitude": 18.50340, "longitude": 73.80420, "criticality": "high",
     "inspection_interval_days": 180, "last_inspected_at": _ago(days=400)},
    {"asset_code": "DR-DEMO-003", "asset_type": "drain", "name": "Storm-water drain inlet (fictional)",
     "latitude": 18.50900, "longitude": 73.80600, "criticality": "medium",
     "inspection_interval_days": 90, "last_inspected_at": _ago(days=30)},
    {"asset_code": "WP-DEMO-002", "asset_type": "waste_point", "name": "Community waste bin point (fictional)",
     "latitude": 18.51050, "longitude": 73.81100, "criticality": "low"},
]
MAINTENANCE = [
    ("SL-DEMO-014", {"event_type": "inspection", "performed_at": _ago(days=40), "outcome": "lamp flickering noted",
                     "performed_by": "Electrical Department (fictional crew)"}),
    ("SL-DEMO-014", {"event_type": "repair", "performed_at": _ago(days=20), "outcome": "lamp unit replaced",
                     "performed_by": "Electrical Department (fictional crew)"}),
    ("RD-DEMO-007", {"event_type": "inspection", "performed_at": _ago(days=400), "outcome": "surface fair",
                     "performed_by": "Public Works (fictional crew)"}),
    ("DR-DEMO-003", {"event_type": "cleaning", "performed_at": _ago(days=30), "outcome": "inlet cleared",
                     "performed_by": "Drainage Department (fictional crew)"}),
]
# (account index, category, description, lat, lng, hours ago, with photo)
REPORTS = [
    (0, "Streetlight Failure", "The streetlight at the school crossing has not worked for three nights. "
        "The road is completely dark when children walk home.", 18.50628, 73.80822, 30, True),
    (1, "Streetlight Failure", "Street light pole at the school junction is off again, very dark and unsafe "
        "for people crossing.", 18.50612, 73.80795, 20, True),
    (2, "Streetlight Failure", "Lamp outside the school gate is not working, dark street at night.",
        18.50635, 73.80801, 8, False),
    (0, "Pothole", "Deep pothole on the road near the bus stop, two-wheelers are swerving around it.",
        18.50355, 73.80431, 5, True),
    (1, "Garbage Accumulation", "Garbage has not been collected for four days and is spilling onto the footpath.",
        18.51040, 73.81090, 50, False),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help="demo database path (default: data/demo/civicmind_demo.db)")
    ap.add_argument("--reset", action="store_true", help="wipe civic data in the DEMO database and rebuild")
    args = ap.parse_args()
    db_path = os.path.abspath(args.db)
    real_db = os.path.abspath(os.path.join(HERE, "civicmind.db"))
    if db_path == real_db:
        sys.exit("Refusing to seed demo data into backend/civicmind.db (the regular database).")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    os.environ["CIVICMIND_DB"] = db_path
    os.environ["CIVICMIND_EVIDENCE_DIR"] = os.path.join(os.path.dirname(db_path), "evidence")
    os.environ["CIVICMIND_DISABLE_AI"] = "1"  # deterministic seeding
    sys.path.insert(0, HERE)

    import database
    from database import query, db, get_meta
    existed = os.path.exists(db_path)
    database.init_db()
    if existed and get_meta("demo_database") != "1" and query("SELECT 1 FROM users LIMIT 1", one=True):
        sys.exit(f"{db_path} contains data and is not marked as a demo database. Refusing to modify it.")
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('demo_database', '1')")

    from config import DEMO_ACCOUNTS
    from auth import create_user
    from services import bootstrap, complaints as intake, assets as asset_svc, risk

    if args.reset:
        bootstrap.reset_data()
        with db() as conn:
            conn.execute("DELETE FROM meta WHERE key IN ('demo_seeded', 'utility_demo_reports', 'workorder_demo')")
        print("Demo civic data cleared.")

    # --- accounts --------------------------------------------------------------
    cred_file = os.path.join(os.path.dirname(db_path), "demo_credentials.txt")
    shared = os.environ.get("CIVICMIND_DEMO_PASSWORD", "")
    if shared and len(shared) < 8:
        sys.exit("CIVICMIND_DEMO_PASSWORD must be at least 8 characters.")
    new_creds = []
    for acc in DEMO_ACCOUNTS:
        if not query("SELECT 1 FROM users WHERE email=?", (acc["email"],), one=True):
            pw = shared or secrets.token_urlsafe(9)
            create_user(acc["name"], acc["email"], pw, role=acc["role"], department=acc.get("department"))
            new_creds.append((acc["role"], acc["email"], pw))
    if new_creds:
        with open(cred_file, "a", encoding="utf-8") as fh:
            fh.write(f"# CivicMind DEMO accounts (fictional) — created {_ago(seconds=0)} UTC\n")
            for role, email, pw in new_creds:
                fh.write(f"{role:8} {email:28} {pw}\n")
        print(f"Created {len(new_creds)} demo account(s); passwords written to {cred_file}")
    users = {a["email"]: query("SELECT id, name, role FROM users WHERE email=?", (a["email"],), one=True)
             for a in DEMO_ACCOUNTS}
    citizens = [users[a["email"]] for a in DEMO_ACCOUNTS if a["role"] == "citizen"]

    # --- assets + maintenance ---------------------------------------------------
    for a in ASSETS:
        if not query("SELECT 1 FROM assets WHERE asset_code=?", (a["asset_code"],), one=True):
            asset_svc.create_asset({**a, "data_source": SOURCE, "is_synthetic": True})
    if not query("SELECT 1 FROM asset_maintenance WHERE data_source=? LIMIT 1", (SOURCE,), one=True):
        with db(immediate=True) as conn:
            for code, m in MAINTENANCE:
                aid = conn.execute("SELECT id FROM assets WHERE asset_code=?", (code,)).fetchone()["id"]
                asset_svc.add_maintenance(conn, aid, asset_svc.validate_maintenance(
                    {**m, "data_source": SOURCE, "is_synthetic": True}))

    # --- complaints (through the REAL intake pipeline) --------------------------
    if get_meta("demo_seeded") != "1":
        for i, (who, cat, text, lat, lng, hours, photo) in enumerate(REPORTS):
            u = citizens[who % len(citizens)]
            # A photo is mandatory for every report; all demo photos are synthetic noise images.
            res = intake.create(u, cat, text, lat, lng, image=_photo_data_url(1000 + i),
                                created_at=_ago(hours=hours), is_synthetic=True)
            print(f"  {res['public_id']}  {cat:22} incident={res['incident_id']}  asset="
                  f"{(res['asset_match'] or {}).get('asset_code')}")
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('demo_seeded', '1')")

    # --- synthetic utility network + fictional water-leak scenario ---------------
    from services.utility_network import demo_data, findings as utility_findings
    with db(immediate=True) as conn:
        added = demo_data.seed(conn)
        moved = demo_data.relocate_leak_reports(conn)
    print(f"Utility network (synthetic, laid along OpenStreetMap streets): {added}")
    if moved:
        # The fictional leak reports moved onto the leak street: refresh the stored utility candidates
        # (system suggestions only — an officer's confirm/reject decision is kept as it is).
        from services.utility_network import matching
        for iid in moved:
            inc = query("SELECT * FROM incidents WHERE id=?", (iid,), one=True)
            found, _ = matching.compute(inc)
            keep = {c["asset"]["id"]: c for c in found}
            with db() as conn:
                for r in conn.execute("SELECT asset_id, status FROM incident_utility_assets WHERE incident_id=?",
                                      (iid,)).fetchall():
                    c = keep.get(r["asset_id"])
                    if c:
                        conn.execute("""UPDATE incident_utility_assets SET distance_m=?, reason=?
                                        WHERE incident_id=? AND asset_id=?""",
                                     (c["distance_m"], c["reason"], iid, r["asset_id"]))
                    elif r["status"] == "candidate":
                        conn.execute("DELETE FROM incident_utility_assets WHERE incident_id=? AND asset_id=? "
                                     "AND status='candidate'", (iid, r["asset_id"]))
            matching.refresh(inc)
        print(f"  moved the fictional leak reports of incident(s) {moved} onto the leak street")
    if get_meta("utility_demo_reports") != "1":
        for who, text, lat, lng, hours in demo_data.LEAK_REPORTS:
            u = citizens[who % len(citizens)]
            lang = "mr" if any("ऀ" <= ch <= "ॿ" for ch in text) else "en"
            res = intake.create(u, "Water Leakage", text, lat, lng, image=_photo_data_url(2000 + who),
                                created_at=_ago(hours=hours), is_synthetic=True, language=lang)
            print(f"  {res['public_id']}  Water Leakage          incident={res['incident_id']}")
            if lang == "mr":
                # A synthetic transcript record (no audio file) to show how officers see voice notes.
                with db() as conn:
                    rid = conn.execute(
                        """INSERT INTO voice_recordings (token, user_id, complaint_id, stored_name, mime, duration_s,
                           sample_rate, bytes, language, detected_language, transcript, transcript_status,
                           transcript_engine, created_at, linked_at)
                           VALUES (?,?,?, NULL, 'audio/wav', 6.0, 16000, 0, 'mr', 'mr-IN', ?, 'transcribed',
                           'synthetic_demo', ?, ?)""",
                        (secrets.token_urlsafe(18), u["id"], res["id"], text, _ago(hours=hours), _ago(hours=hours))).lastrowid
                    conn.execute("UPDATE complaints SET voice_recording_id=? WHERE id=?", (rid, res["id"]))
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('utility_demo_reports', '1')")

    # --- fictional contractor + a scheduled work order on the streetlight incident -------
    with db() as conn:
        conn.execute("""INSERT OR IGNORE INTO contractors (contractor_code, name, department, contact_person, contact_role,
                        phone, office_address, verification_status, is_synthetic, created_at)
                        VALUES ('CON-DEMO-01', 'Sahyadri Civil Works (FICTIONAL)', 'Water Supply Department',
                        'Site supervisor (demo)', 'Supervisor', 'DEMO-ONLY · not a real number',
                        'Fictional office, Pune (demo)', 'unverified', 1, ?)""", (_ago(days=1),))
        conn.execute("""INSERT OR IGNORE INTO contractors (contractor_code, name, department, contact_person, contact_role,
                        phone, office_address, verification_status, is_synthetic, created_at)
                        VALUES ('CON-DEMO-02', 'Deccan Electricals (FICTIONAL)', 'Electrical Department',
                        'Lineman lead (demo)', 'Team lead', 'DEMO-ONLY · not a real number',
                        'Fictional office, Pune (demo)', 'unverified', 1, ?)""", (_ago(days=1),))
    if get_meta("workorder_demo") != "1":
        from services import actions as action_svc, workorders as wo_svc
        officer = query("SELECT * FROM users WHERE role='officer' ORDER BY id LIMIT 1", one=True)
        inc = query("""SELECT id FROM incidents WHERE category='Streetlight Failure' AND status != 'resolved'
                       ORDER BY id LIMIT 1""", one=True)
        if officer and inc:
            act = query("SELECT id, status FROM actions WHERE incident_id=? ORDER BY id DESC LIMIT 1", (inc["id"],), one=True)
            if act and act["status"] == "pending":
                action_svc.approve_action(act["id"], officer)
            con = query("SELECT id FROM contractors WHERE contractor_code='CON-DEMO-02'", one=True)
            try:
                wo = wo_svc.create(inc["id"], officer, {"task": "Replace the failed lamp unit and test the feeder pillar",
                                                         "contractor_id": con["id"], "priority": "high",
                                                         "est_start": _ago(hours=-2), "est_completion": _ago(hours=-30),
                                                         "utility_asset_ids": ["TR-DEMO-01"]})
                print(f"  {wo['public_id']}  work order for the streetlight incident (fictional contractor)")
            except wo_svc.WorkOrderError as e:
                print(f"  work order not created: {e.message}")
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('workorder_demo', '1')")
    print("Utility findings:", utility_findings.safe_scan())
    summary = risk.scan()
    sample = os.path.join(os.path.dirname(db_path), "sample_proof_photo.jpg")
    if not os.path.exists(sample):
        _photo_data_url(424242, path=sample)
    blank = os.path.join(os.path.dirname(db_path), "blank_test_photo.png")
    if not os.path.exists(blank):  # for showing that blank proof photos are rejected
        from PIL import Image
        Image.new("RGB", (640, 480), (128, 128, 128)).save(blank)

    print("\nDemo database ready:", db_path)
    print("  incidents:", query("SELECT COUNT(*) c FROM incidents", one=True)["c"],
          "| complaints:", query("SELECT COUNT(*) c FROM complaints", one=True)["c"],
          "| assets:", query("SELECT COUNT(*) c FROM assets", one=True)["c"],
          "| open risk findings:", query("SELECT COUNT(*) c FROM risk_findings WHERE status='open'", one=True)["c"],
          f"(scan: {summary})")
    print("  logins:", cred_file)
    print("  synthetic proof photo for the officer step:", sample)
    print("  blank photo (should be REJECTED as proof):", blank)


if __name__ == "__main__":
    main()
