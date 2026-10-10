"""
CivicMind — utility types, asset types and their network semantics.

Different networks behave differently, so tracing rules are per utility:
  * water / gas / electric: directed supply from recorded sources, with
    control devices (valves, switches, breakers) that operators can use;
  * sewer / storm: directed gravity flow towards outfalls; no isolation
    devices are modelled (manholes are access points, not controls);
  * telecom / road: undirected links; no flow and no isolation.
"""

UTILITY_TYPES = {
    "water":    {"label": "Water supply", "color": "#1F6FE5", "flow": "supply",
                 "control_types": ["valve"], "source_types": ["source", "reservoir", "pump_station"]},
    "sewer":    {"label": "Sewerage", "color": "#7A4FB5", "flow": "gravity",
                 "control_types": [], "source_types": []},
    "storm":    {"label": "Stormwater drainage", "color": "#0F9D91", "flow": "gravity",
                 "control_types": [], "source_types": []},
    "electric": {"label": "Electricity", "color": "#D08A00", "flow": "supply",
                 "control_types": ["switch", "breaker", "ring_main_unit"], "source_types": ["substation"]},
    "gas":      {"label": "Gas", "color": "#C93C3C", "flow": "supply",
                 "control_types": ["valve", "regulator"], "source_types": ["city_gate"]},
    "telecom":  {"label": "Telecom / fibre", "color": "#5B6B82", "flow": "none",
                 "control_types": [], "source_types": ["exchange"]},
    "road":     {"label": "Roads / road works", "color": "#8A97A8", "flow": "none",
                 "control_types": [], "source_types": []},
}

LINE_TYPES = {"main", "branch", "service_line", "trunk_sewer", "sewer_line", "storm_drain", "culvert", "cable",
              "feeder", "overhead_line", "gas_main", "gas_service", "fibre", "duct", "road_segment", "road_works"}
POINT_TYPES = {"source", "reservoir", "pump_station", "junction", "valve", "hydrant", "manhole", "outfall",
               "treatment_plant", "substation", "transformer", "switch", "breaker", "ring_main_unit", "regulator",
               "city_gate", "exchange", "cabinet", "chamber", "endpoint", "meter", "inlet"}
ASSET_TYPES = LINE_TYPES | POINT_TYPES

CONNECTION_TYPES = ("feeds", "joins", "controls", "drains_to", "links")
DIRECTIONS = ("downstream", "bidirectional", "unknown")
OPERATIONAL_STATUSES = ("in_service", "out_of_service", "under_repair", "planned", "abandoned")
CRITICALITIES = ("low", "medium", "high", "critical")

# Technical attributes accepted from a source. Anything else is ignored, and a
# missing value stays missing — it is never filled in.
ATTRIBUTE_KEYS = {"diameter_mm", "material", "install_year", "depth_m", "voltage_kv", "pressure_class",
                  "cable_type", "length_m", "inspection_interval_days", "last_inspected_on", "capacity"}

# Complaint category -> utility types that could plausibly be involved.
CATEGORY_UTILITIES = {
    "Water Leakage": ["water"], "Low Water Pressure": ["water"],
    "Sewage Overflow": ["sewer"], "Drainage Issue": ["storm", "sewer"],
    "Streetlight Failure": ["electric"], "Traffic Signal Failure": ["electric"],
    "Pothole": ["road"], "Road Damage": ["road"],
}
MATCH_RADIUS_M = 120

PROVENANCE_LABELS = {
    "verified_as_built": "Verified as-built",
    "official_unverified": "Official source — not field verified",
    "planned_design": "Planned / design",
    "approximate_unverified": "Approximate / unverified",
    "synthetic_demo": "Synthetic demo (fictional)",
}


def is_control(utility_type, asset_type):
    return asset_type in UTILITY_TYPES.get(utility_type, {}).get("control_types", [])


def is_source(utility_type, asset_type):
    return asset_type in UTILITY_TYPES.get(utility_type, {}).get("source_types", [])


def has_isolation(utility_type):
    return bool(UTILITY_TYPES.get(utility_type, {}).get("control_types"))
