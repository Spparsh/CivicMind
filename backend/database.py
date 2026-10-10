"""
CivicMind — SQLite persistence layer.

A single-file SQLite database keeps the prototype zero-setup. Every helper
opens a short-lived connection, which is safe under uvicorn's threadpool.

Schema changes are ADDITIVE ONLY: `init_db()` creates missing tables, adds
missing columns and indexes, and backfills new columns. It never drops or
recreates a table, so it is safe to run on every start against real data.

Timestamps are stored as UTC text in SQLite's own format
('YYYY-MM-DD HH:MM:SS') so string comparison == chronological comparison.
"""
import contextlib
import secrets
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'citizen',   -- citizen | officer | admin
    phone         TEXT,
    department    TEXT,
    ward          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS complaints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id     TEXT NOT NULL UNIQUE,              -- CMP-00123 (internal reference)
    user_id       INTEGER REFERENCES users(id),
    category      TEXT NOT NULL,
    description   TEXT NOT NULL,
    latitude      REAL NOT NULL,
    longitude     REAL NOT NULL,
    address       TEXT,
    ward          TEXT,
    zone          TEXT,
    image_path    TEXT,                              -- stored evidence file name (private)
    image_hash    TEXT,
    proof_verified INTEGER,
    proof_check   TEXT,
    status        TEXT NOT NULL DEFAULT 'submitted', -- submitted|clustered|in_progress|resolved|rejected
    severity      INTEGER NOT NULL DEFAULT 3,
    ai_category   TEXT,
    ai_confidence REAL,
    ai_summary    TEXT,
    ai_engine     TEXT,                              -- sarvam | rules
    incident_id   INTEGER REFERENCES incidents(id),
    resolution_proof_path  TEXT,
    resolution_proof_check TEXT,
    resolution_image_hash  TEXT,
    resolved_by            INTEGER REFERENCES users(id),
    resolved_at            TEXT,
    resolution_notes       TEXT,
    resolution_plan        TEXT,
    eta_hours              REAL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS incidents (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id          TEXT NOT NULL UNIQUE,         -- INC-00042
    title              TEXT NOT NULL,
    type               TEXT NOT NULL,
    category           TEXT NOT NULL,
    confidence         REAL NOT NULL DEFAULT 0,
    severity           INTEGER NOT NULL DEFAULT 3,
    priority_score     REAL NOT NULL DEFAULT 0,
    priority_label     TEXT NOT NULL DEFAULT 'Medium',
    affected_people    INTEGER NOT NULL DEFAULT 0,   -- = distinct reporting citizens
    spread_km          REAL NOT NULL DEFAULT 0,
    duration_hours     REAL NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active',
    ward               TEXT,
    zone               TEXT,
    centroid_lat       REAL NOT NULL,
    centroid_lng       REAL NOT NULL,
    report_count       INTEGER NOT NULL DEFAULT 0,
    summary            TEXT,
    probable_cause     TEXT,
    priority_reason    TEXT,
    investigation      TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS incident_complaints (
    incident_id  INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    complaint_id INTEGER NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
    PRIMARY KEY (incident_id, complaint_id)
);

CREATE TABLE IF NOT EXISTS actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    action_text   TEXT NOT NULL,
    assigned_team TEXT,
    department    TEXT,
    eta_hours     REAL DEFAULT 24,
    checklist     TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|in_progress|completed|rejected
    approved_by   INTEGER REFERENCES users(id),
    notes         TEXT,
    proof_path    TEXT,
    proof_check   TEXT,
    image_hash    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at   TEXT,
    started_at    TEXT,
    completed_at  TEXT
);

CREATE TABLE IF NOT EXISTS evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    evidence_type TEXT,
    value         TEXT,
    source        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS resolutions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id          INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    before_metric        REAL,
    after_metric         REAL,
    reduction_pct        REAL,
    resolution_confidence REAL,
    summary              TEXT,
    proof_path           TEXT,
    proof_check          TEXT,
    image_hash           TEXT,
    resolved_by          INTEGER REFERENCES users(id),
    status               TEXT DEFAULT 'verified',
    verified_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER REFERENCES users(id),
    complaint_id INTEGER REFERENCES complaints(id),
    incident_id  INTEGER REFERENCES incidents(id),
    title        TEXT NOT NULL,
    message      TEXT,
    type         TEXT DEFAULT 'info',
    is_read      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT,
    action     TEXT,
    detail     TEXT,
    incident_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Infrastructure intelligence (added in v3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_code               TEXT NOT NULL UNIQUE,   -- e.g. SL-014
    asset_type               TEXT NOT NULL,          -- streetlight | road_segment | drain | ...
    name                     TEXT NOT NULL,
    latitude                 REAL NOT NULL,
    longitude                REAL NOT NULL,
    ward                     TEXT,
    department               TEXT,
    operational_status       TEXT NOT NULL DEFAULT 'operational',
    criticality              TEXT NOT NULL DEFAULT 'medium',  -- low|medium|high|critical
    criticality_reason       TEXT,
    inspection_interval_days INTEGER,
    last_inspected_at        TEXT,
    data_source              TEXT NOT NULL,
    is_synthetic             INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS asset_maintenance (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id      INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    event_type    TEXT NOT NULL,          -- inspection | repair | replacement | cleaning
    performed_at  TEXT NOT NULL,
    outcome       TEXT,
    performed_by  TEXT,                   -- team / department (free text)
    notes         TEXT,
    data_source   TEXT NOT NULL,
    is_synthetic  INTEGER NOT NULL DEFAULT 0,
    recorded_by   INTEGER REFERENCES users(id),
    action_id     INTEGER REFERENCES actions(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS complaint_assets (
    complaint_id INTEGER PRIMARY KEY REFERENCES complaints(id) ON DELETE CASCADE,
    asset_id     INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    distance_m   REAL,
    method       TEXT NOT NULL,
    reason       TEXT,
    status       TEXT NOT NULL DEFAULT 'auto',   -- auto | confirmed | rejected
    reviewed_by  INTEGER REFERENCES users(id),
    matched_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS risk_findings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_key      TEXT NOT NULL UNIQUE,      -- rule + subject ids (idempotent upsert)
    rule_id          TEXT NOT NULL,
    finding_type     TEXT NOT NULL,
    severity         TEXT NOT NULL,             -- low|medium|high|critical
    asset_id         INTEGER REFERENCES assets(id) ON DELETE CASCADE,
    incident_id      INTEGER REFERENCES incidents(id) ON DELETE CASCADE,
    supporting       TEXT,                      -- JSON {complaint_ids, maintenance_ids, ...}
    explanation      TEXT NOT NULL,
    recommended_step TEXT,
    status           TEXT NOT NULL DEFAULT 'open',  -- open|acknowledged|dismissed|cleared
    is_synthetic     INTEGER NOT NULL DEFAULT 0,
    detected_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_by      INTEGER REFERENCES users(id),
    reviewed_at      TEXT,
    review_note      TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---------------------------------------------------------------------------
-- Public data-source discovery (TinyFish). CANDIDATE EVIDENCE ONLY: nothing
-- in these tables is an infrastructure record. Utility assets are created
-- only through the utility import preview + admin confirmation.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_discovery_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    authority     TEXT,
    region        TEXT,
    topic         TEXT,
    data_types    TEXT,                       -- JSON list
    query         TEXT NOT NULL,              -- query actually sent to TinyFish Search
    provider      TEXT NOT NULL,              -- tinyfish_search
    provider_mode TEXT NOT NULL,              -- live | mock
    status        TEXT NOT NULL,              -- completed | failed
    error_code    TEXT,
    result_count  INTEGER NOT NULL DEFAULT 0,
    requested_by  INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS source_candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES source_discovery_runs(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    title         TEXT,
    snippet       TEXT,
    site_name     TEXT,
    result_date   TEXT,
    position      INTEGER,
    discovery_query TEXT,
    provenance    TEXT,                       -- JSON {provider, provider_mode, query, retrieved_at}
    review_status TEXT NOT NULL DEFAULT 'candidate',   -- candidate | selected | rejected
    reviewed_by   INTEGER REFERENCES users(id),
    reviewed_at   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (run_id, url)
);

CREATE TABLE IF NOT EXISTS source_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER REFERENCES source_candidates(id) ON DELETE SET NULL,
    method          TEXT NOT NULL,            -- tinyfish_fetch | tinyfish_agent
    url             TEXT NOT NULL,
    final_url       TEXT,
    title           TEXT,
    description     TEXT,
    language        TEXT,
    published_date  TEXT,
    content_format  TEXT,
    content_text    TEXT,                     -- capped; untrusted
    content_chars   INTEGER NOT NULL DEFAULT 0,
    content_truncated INTEGER NOT NULL DEFAULT 0,
    content_sha256  TEXT,
    fetch_status    TEXT NOT NULL,            -- ok | error
    error_code      TEXT,
    error_status    INTEGER,
    warnings        TEXT,                     -- JSON list
    provider_mode   TEXT NOT NULL,            -- live | mock
    run_ref         TEXT,                     -- TinyFish agent run id, if any
    requested_by    INTEGER REFERENCES users(id),
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now')),
    review_status   TEXT NOT NULL DEFAULT 'pending_review',  -- pending_review | reviewed | rejected
    reviewed_by     INTEGER REFERENCES users(id),
    reviewed_at     TEXT,
    review_note     TEXT
);

CREATE TABLE IF NOT EXISTS source_agent_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL,
    status        TEXT NOT NULL,              -- COMPLETED | FAILED | ERROR
    run_ref       TEXT,
    error_code    TEXT,
    provider_mode TEXT NOT NULL,
    requested_by  INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Departments (seeded from data/civicmind_config.json; admins add/deactivate).
CREATE TABLE IF NOT EXISTS departments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    contact_role TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Contractors / service providers that carry out work orders.
CREATE TABLE IF NOT EXISTS contractors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    contractor_code  TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    department       TEXT,
    contact_person   TEXT,
    contact_role     TEXT,
    phone            TEXT,
    email            TEXT,
    office_address   TEXT,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    is_synthetic     INTEGER NOT NULL DEFAULT 0,
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_by       INTEGER REFERENCES users(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Work orders: the execution layer under an approved response plan (action).
-- assigned -> in_progress -> awaiting_verification -> verified
--                      ^------------- rejected by officer --'
-- assigned | in_progress -> cancelled
CREATE TABLE IF NOT EXISTS work_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id        TEXT NOT NULL UNIQUE,          -- WO-00012
    incident_id      INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    action_id        INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    utility_asset_ids TEXT,                         -- JSON list of utility asset_id strings
    department       TEXT,
    contractor_id    INTEGER REFERENCES contractors(id),
    team_name        TEXT,
    task             TEXT NOT NULL,
    priority         TEXT NOT NULL DEFAULT 'medium',
    status           TEXT NOT NULL DEFAULT 'assigned',
    est_start        TEXT,
    est_completion   TEXT,
    actual_start     TEXT,
    actual_completion TEXT,
    required_evidence TEXT NOT NULL DEFAULT 'after_photo',
    verification_note TEXT,
    created_by       INTEGER REFERENCES users(id),
    verified_by      INTEGER REFERENCES users(id),
    verified_at      TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS work_order_updates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,      -- created | started | progress | estimate_changed | submitted | accepted | rejected | cancelled | evidence
    from_status   TEXT,
    to_status     TEXT,
    note          TEXT,
    old_value     TEXT,
    new_value     TEXT,
    actor_id      INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS work_order_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,      -- before | after
    stored_name   TEXT NOT NULL,
    image_hash    TEXT NOT NULL,
    check_json    TEXT NOT NULL,
    note          TEXT,
    uploaded_by   INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Short voice descriptions (private audio; transcript stored separately).
CREATE TABLE IF NOT EXISTS voice_recordings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    token            TEXT NOT NULL UNIQUE,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    complaint_id     INTEGER REFERENCES complaints(id) ON DELETE SET NULL,
    stored_name      TEXT,                 -- NULL once the audio is deleted by the retention policy
    mime             TEXT NOT NULL,
    duration_s       REAL NOT NULL,
    sample_rate      INTEGER,
    bytes            INTEGER NOT NULL,
    language         TEXT,                 -- language the citizen selected
    detected_language TEXT,                -- provider-reported language, if any
    transcript       TEXT,                 -- AI-generated, unedited
    transcript_status TEXT NOT NULL,       -- transcribed | unavailable | failed | empty
    transcript_engine TEXT,
    error_code       TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    linked_at        TEXT,
    audio_deleted_at TEXT
);

-- Up to MEDIA_MAX_FILES photos/videos per complaint. Uploaded first (validated,
-- private), then claimed by the complaint with a single-use token.
CREATE TABLE IF NOT EXISTS complaint_media (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    complaint_id INTEGER REFERENCES complaints(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL,          -- photo | video
    stored_name  TEXT NOT NULL,
    mime         TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    width        INTEGER,
    height       INTEGER,
    duration_s   REAL,                   -- NULL when the video file does not declare it
    sha256       TEXT NOT NULL,
    check_json   TEXT,                   -- photo: automated check result
    position     INTEGER,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    linked_at    TEXT
);

-- Officer-recorded coordination with utility operators (water-leak response
-- etc.). Human-entered facts only: the app never operates a utility.
CREATE TABLE IF NOT EXISTS incident_operator_updates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    step          TEXT NOT NULL,
    operator_org  TEXT,
    contact_ref   TEXT,                       -- UtilityContact.contact_id after the utility merge
    asset_ref     TEXT,                       -- UtilityAsset.asset_id after the utility merge
    reference     TEXT,                       -- operator ticket / guidance reference
    note          TEXT,
    recorded_by   INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Columns added after v2 — applied automatically to existing databases.
MIGRATIONS = {
    "users":       [("token_version", "INTEGER NOT NULL DEFAULT 0"),
                    ("is_active", "INTEGER NOT NULL DEFAULT 1"),
                    ("preferred_language", "TEXT")],
    "complaints":  [("image_hash", "TEXT"), ("proof_verified", "INTEGER"), ("proof_check", "TEXT"),
                    ("resolution_proof_path", "TEXT"), ("resolution_proof_check", "TEXT"),
                    ("resolution_image_hash", "TEXT"), ("resolved_by", "INTEGER"),
                    ("resolved_at", "TEXT"), ("resolution_notes", "TEXT"),
                    ("resolution_plan", "TEXT"), ("eta_hours", "REAL"),
                    ("tracking_code", "TEXT"), ("is_synthetic", "INTEGER NOT NULL DEFAULT 0"),
                    # Multilingual intake: `description` is ALWAYS the citizen's original text.
                    ("original_language", "TEXT"), ("detected_language", "TEXT"),
                    ("description_translation_en", "TEXT"), ("translation_engine", "TEXT"),
                    ("assistant_engine", "TEXT"),
                    # Location provenance: how the pin was set, and any reverse-geocoded address (kept apart
                    # from the citizen-typed `address`).
                    ("location_source", "TEXT"), ("location_accuracy_m", "REAL"), ("geocoded_address", "TEXT"),
                    ("voice_recording_id", "INTEGER")],
    "incidents":   [("priority_factors", "TEXT"), ("asset_id", "INTEGER"),
                    ("last_report_at", "TEXT"), ("resolved_at", "TEXT"),
                    ("grouping_basis", "TEXT")],
    "actions":     [("proof_path", "TEXT"), ("proof_check", "TEXT"), ("image_hash", "TEXT"),
                    ("source", "TEXT"), ("created_by", "INTEGER"), ("rejected_by", "INTEGER"),
                    ("rejected_at", "TEXT"), ("started_by", "INTEGER"),
                    ("completed_by", "INTEGER"), ("rejection_reason", "TEXT")],
    "resolutions": [("proof_path", "TEXT"), ("proof_check", "TEXT"), ("image_hash", "TEXT"),
                    ("resolved_by", "INTEGER"), ("complaint_id", "INTEGER"),
                    ("action_id", "INTEGER"), ("verification_type", "TEXT")],
    "activity":    [("actor_id", "INTEGER"), ("complaint_id", "INTEGER")],
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_complaints_user ON complaints(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_complaints_status ON complaints(status)",
    "CREATE INDEX IF NOT EXISTS ix_complaints_incident ON complaints(incident_id)",
    "CREATE INDEX IF NOT EXISTS ix_complaints_created ON complaints(created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_complaints_tracking ON complaints(tracking_code)",
    "CREATE INDEX IF NOT EXISTS ix_incidents_status ON incidents(status)",
    "CREATE INDEX IF NOT EXISTS ix_actions_incident ON actions(incident_id)",
    "CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications(user_id, is_read)",
    "CREATE INDEX IF NOT EXISTS ix_activity_incident ON activity(incident_id)",
    "CREATE INDEX IF NOT EXISTS ix_assets_type ON assets(asset_type)",
    "CREATE INDEX IF NOT EXISTS ix_maint_asset ON asset_maintenance(asset_id, performed_at)",
    "CREATE INDEX IF NOT EXISTS ix_casset_asset ON complaint_assets(asset_id)",
    "CREATE INDEX IF NOT EXISTS ix_findings_status ON risk_findings(status)",
    "CREATE INDEX IF NOT EXISTS ix_candidates_run ON source_candidates(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_snapshots_review ON source_snapshots(review_status, fetched_at)",
    "CREATE INDEX IF NOT EXISTS ix_agent_runs_created ON source_agent_runs(created_at)",
    "CREATE INDEX IF NOT EXISTS ix_operator_updates_incident ON incident_operator_updates(incident_id)",
    "CREATE INDEX IF NOT EXISTS ix_wo_incident ON work_orders(incident_id)",
    "CREATE INDEX IF NOT EXISTS ix_wo_status ON work_orders(status)",
    "CREATE INDEX IF NOT EXISTS ix_wo_updates ON work_order_updates(work_order_id)",
    "CREATE INDEX IF NOT EXISTS ix_wo_evidence ON work_order_evidence(work_order_id)",
    "CREATE INDEX IF NOT EXISTS ix_wo_evidence_hash ON work_order_evidence(image_hash)",
    "CREATE INDEX IF NOT EXISTS ix_voice_user ON voice_recordings(user_id, complaint_id)",
    "CREATE INDEX IF NOT EXISTS ix_media_complaint ON complaint_media(complaint_id, position)",
    "CREATE INDEX IF NOT EXISTS ix_media_user ON complaint_media(user_id, complaint_id)",
    "CREATE INDEX IF NOT EXISTS ix_media_hash ON complaint_media(sha256)",
]


def _db_path():
    # Read at call time so tests (and the demo seeder) can point elsewhere.
    return config.DB_PATH


def get_conn():
    conn = sqlite3.connect(_db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextlib.contextmanager
def db(immediate=False):
    """Transaction scope. `immediate=True` takes the write lock up front, so a
    read-check-write sequence (state transitions) cannot interleave with
    another writer."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def query(sql, params=(), one=False):
    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return (dict(rows[0]) if rows else None) if one else [dict(r) for r in rows]
    finally:
        conn.close()


def execute(sql, params=()):
    with db() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def now():
    """UTC timestamp in SQLite's datetime('now') format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_ts(ts):
    """Parse either 'YYYY-MM-DD HH:MM:SS' or ISO 'YYYY-MM-DDTHH:MM:SS' (legacy)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "").replace("T", " ").strip()[:19])
    except ValueError:
        return None


def new_tracking_code():
    # 10 chars from an unambiguous alphabet ≈ 50 bits — not enumerable.
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def insert_with_public_id(conn, table, prefix, columns, values, width=5):
    """Insert a row and derive its public id from the AUTOINCREMENT key.

    AUTOINCREMENT ids are never reused, so `PREFIX-<id>` is unique even under
    concurrent inserts, deletions or a data reset (unlike COUNT(*)+1).
    """
    placeholder = f"TMP-{secrets.token_hex(8)}"
    cols = ", ".join(["public_id", *columns])
    qs = ", ".join(["?"] * (len(columns) + 1))
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", (placeholder, *values))
    row_id = cur.lastrowid
    public_id = f"{prefix}-{row_id:0{width}d}"
    conn.execute(f"UPDATE {table} SET public_id=? WHERE id=?", (public_id, row_id))
    return row_id, public_id


def _migrate(conn):
    for table, cols in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, coltype in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
    # Backfill private tracking codes for complaints filed before v3.
    for row in conn.execute("SELECT id FROM complaints WHERE tracking_code IS NULL").fetchall():
        conn.execute("UPDATE complaints SET tracking_code=? WHERE id=?",
                     (new_tracking_code(), row["id"]))
    # Normalise legacy ISO timestamps ('T' separator) written by older versions
    # so string comparisons stay chronological.
    for table, cols in {
        "complaints": ["resolved_at", "updated_at"],
        "incidents": ["updated_at"],
        "actions": ["approved_at", "started_at", "completed_at"],
    }.items():
        for col in cols:
            conn.execute(f"UPDATE {table} SET {col}=replace({col},'T',' ') WHERE {col} LIKE '____-__-__T%'")
    for ddl in INDEXES:
        conn.execute(ddl)
    # Departments named in the configuration always exist (admins can add more or deactivate them).
    for name in sorted(set(config.DEPARTMENT_MAP.values())):
        conn.execute("INSERT OR IGNORE INTO departments (name) VALUES (?)", (name,))


def init_db():
    conn = get_conn()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate(conn)
            # Utility network tables (additive, idempotent).
            from services.utility_network.schema import init_schema
            init_schema(conn)
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '4')")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def get_meta(key):
    row = query("SELECT value FROM meta WHERE key=?", (key,), one=True)
    return row["value"] if row else None


def is_demo_db():
    return get_meta("demo_database") == "1"
