"""
CivicMind — utility network schema (additive, idempotent).

Geometry (where an asset is drawn), connectivity (what is recorded as
connected) and operational status are stored separately:

  utility_assets       typed assets; Point (sources, junctions, valves, switches,
                       transformers...) or LineString (mains, cables, sewers...)
  utility_connections  explicit recorded relationships between two assets.
                       A geometric near-miss is NEVER a connection.
  utility_contacts     operator / department contacts with verification state
  utility_maintenance  maintenance and inspection events per asset
  utility_data_sources where records came from (file, web page, synthetic...)
  utility_import_jobs  preview -> confirm import pipeline
  utility_findings     rule-triggered risk / data-quality findings
  incident_utility_assets  incident <-> asset associations (candidate/confirmed)

Point assets play the role of network nodes (sources, junctions, endpoints),
so nodes and assets share one registry and one ID space.
All tables are prefixed `utility_` (contract proposal P1) to avoid the legacy
`assets` / `asset_maintenance` / `risk_findings` tables.
"""

PROVENANCE_CLASSES = ("verified_as_built", "official_unverified", "planned_design",
                      "approximate_unverified", "synthetic_demo")
VERIFICATION_STATUSES = ("verified", "unverified", "disputed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS utility_data_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_code     TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    source_type     TEXT NOT NULL,          -- synthetic | file_import | web_page | agent_extraction | official_service
    authority       TEXT,
    source_url      TEXT,
    file_name       TEXT,
    license_notes   TEXT,
    crs             TEXT,
    provenance_class TEXT NOT NULL,
    snapshot_id     INTEGER,                -- source_snapshots.id when discovered via TinyFish
    is_synthetic    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',
    reviewed_by     INTEGER REFERENCES users(id),
    reviewed_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS utility_assets (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id           TEXT NOT NULL UNIQUE,     -- stable public identifier, e.g. WM-DEMO-03
    utility_type       TEXT NOT NULL,            -- water | sewer | storm | electric | gas | telecom | road
    asset_type         TEXT NOT NULL,            -- main | branch | source | junction | valve | cable | switch ...
    subtype            TEXT,
    name               TEXT NOT NULL,
    geometry_type      TEXT NOT NULL,            -- Point | LineString
    geometry           TEXT NOT NULL,            -- GeoJSON geometry (EPSG:4326, lng/lat order)
    min_lat REAL NOT NULL, min_lng REAL NOT NULL, max_lat REAL NOT NULL, max_lng REAL NOT NULL,
    operational_status TEXT,                     -- NULL = not recorded
    operator_org       TEXT,
    department         TEXT,
    criticality        TEXT,                     -- NULL = not sourced
    attributes         TEXT,                     -- JSON of SOURCED technical attributes only
    is_control_device  INTEGER NOT NULL DEFAULT 0,
    is_source          INTEGER NOT NULL DEFAULT 0,
    sensitivity        TEXT NOT NULL DEFAULT 'restricted',
    source_id          INTEGER REFERENCES utility_data_sources(id),
    source_record_id   TEXT,
    provenance_class   TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    is_synthetic       INTEGER NOT NULL DEFAULT 0,
    import_job_id      INTEGER,
    source_updated_at  TEXT,
    last_verified_at   TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS utility_connections (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id      TEXT NOT NULL UNIQUE,
    from_asset         INTEGER NOT NULL REFERENCES utility_assets(id) ON DELETE CASCADE,
    to_asset           INTEGER NOT NULL REFERENCES utility_assets(id) ON DELETE CASCADE,
    connection_type    TEXT NOT NULL,            -- feeds | joins | controls | drains_to | links
    direction          TEXT NOT NULL,            -- downstream (flow from->to) | bidirectional | unknown
    source_id          INTEGER REFERENCES utility_data_sources(id),
    provenance_class   TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    is_synthetic       INTEGER NOT NULL DEFAULT 0,
    valid_from         TEXT,
    valid_to           TEXT,
    notes              TEXT,
    import_job_id      INTEGER,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (from_asset != to_asset),
    UNIQUE (from_asset, to_asset, connection_type)
);

CREATE TABLE IF NOT EXISTS utility_contacts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id         TEXT NOT NULL UNIQUE,
    organization       TEXT NOT NULL,
    role               TEXT NOT NULL,
    phone              TEXT,
    email              TEXT,
    is_emergency       INTEGER NOT NULL DEFAULT 0,
    hours              TEXT,
    utility_type       TEXT,                     -- applies to assets of this utility type ...
    operator_org       TEXT,                     -- ... operated by this organisation
    asset_id           INTEGER REFERENCES utility_assets(id) ON DELETE SET NULL,  -- or one specific asset
    source_id          INTEGER REFERENCES utility_data_sources(id),
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    last_verified_at   TEXT,
    visibility_level   TEXT NOT NULL DEFAULT 'officer',   -- officer | admin
    is_synthetic       INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS utility_maintenance (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id          TEXT NOT NULL UNIQUE,
    asset_id           INTEGER NOT NULL REFERENCES utility_assets(id) ON DELETE CASCADE,
    work_type          TEXT NOT NULL,            -- inspection | repair | replacement | flushing | ...
    performed_on       TEXT NOT NULL,
    outcome            TEXT,
    reference          TEXT,                     -- work order / incident reference
    department         TEXT,
    evidence_ref       TEXT,
    source_id          INTEGER REFERENCES utility_data_sources(id),
    provenance_class   TEXT NOT NULL,
    is_synthetic       INTEGER NOT NULL DEFAULT 0,
    recorded_by        INTEGER REFERENCES users(id),
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS utility_import_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_code        TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL,              -- previewed | committed | failed | discarded
    input_format    TEXT NOT NULL,              -- geojson | csv
    source_meta     TEXT NOT NULL,              -- JSON DataSource metadata
    payload         TEXT,                       -- JSON normalized, validated records (until committed)
    payload_sha256  TEXT,
    summary         TEXT NOT NULL,              -- JSON accepted / rejected / duplicates / missing relationships
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    committed_by    INTEGER REFERENCES users(id),
    committed_at    TEXT,
    data_source_id  INTEGER REFERENCES utility_data_sources(id)
);

CREATE TABLE IF NOT EXISTS utility_findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_key     TEXT NOT NULL UNIQUE,
    rule_id         TEXT NOT NULL,
    severity        TEXT NOT NULL,              -- low | medium | high | critical
    asset_ids       TEXT NOT NULL,              -- JSON list of utility asset_id strings
    incident_ids    TEXT,                       -- JSON list of incident ids
    supporting      TEXT,                       -- JSON {complaint_ids, maintenance_ids, connection_ids}
    explanation     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',   -- open | acknowledged | dismissed | cleared
    is_synthetic    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_by     INTEGER REFERENCES users(id),
    reviewed_at     TEXT,
    review_note     TEXT
);

CREATE TABLE IF NOT EXISTS incident_utility_assets (
    incident_id   INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    asset_id      INTEGER NOT NULL REFERENCES utility_assets(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'candidate',   -- candidate | confirmed | rejected
    match_method  TEXT NOT NULL,
    distance_m    REAL,
    reason        TEXT,
    reviewed_by   INTEGER REFERENCES users(id),
    reviewed_at   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (incident_id, asset_id)
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_uassets_type ON utility_assets(utility_type, asset_type)",
    "CREATE INDEX IF NOT EXISTS ix_uassets_bbox ON utility_assets(min_lat, max_lat, min_lng, max_lng)",
    "CREATE INDEX IF NOT EXISTS ix_uconn_from ON utility_connections(from_asset)",
    "CREATE INDEX IF NOT EXISTS ix_uconn_to ON utility_connections(to_asset)",
    "CREATE INDEX IF NOT EXISTS ix_umaint_asset ON utility_maintenance(asset_id, performed_on)",
    "CREATE INDEX IF NOT EXISTS ix_ucontacts_scope ON utility_contacts(utility_type, operator_org)",
    "CREATE INDEX IF NOT EXISTS ix_ufindings_status ON utility_findings(status)",
    "CREATE INDEX IF NOT EXISTS ix_iua_asset ON incident_utility_assets(asset_id)",
]


def init_schema(conn):
    """Idempotent: CREATE ... IF NOT EXISTS only. Safe on every start and on real data."""
    for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
        conn.execute(stmt)
    for ddl in INDEXES:
        conn.execute(ddl)
