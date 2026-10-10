"""
CivicMind — public configuration endpoints (no secrets, no passwords).
"""
from fastapi import APIRouter

from config import (CATEGORIES, DEPARTMENT_MAP, WARDS_RAW, CITY_NAME, CITY_CENTER,
                    CLUSTER_RADIUS_KM, CLUSTER_TIME_WINDOW_HOURS, MIN_CLUSTER_SIZE,
                    REQUIRE_ISSUE_PHOTO, SERVICE_RADIUS_KM, ASSET_MATCH_RADIUS_M)
from services import geo

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config")
def get_config():
    return {
        "city_name": CITY_NAME,
        "city_center": CITY_CENTER,
        "service_radius_km": SERVICE_RADIUS_KM,
        "categories": CATEGORIES,
        "departments": sorted(set(DEPARTMENT_MAP.values())),
        "department_map": DEPARTMENT_MAP,
        "wards": WARDS_RAW,
        "ward_method": geo.WARD_METHOD,
        "require_issue_photo": REQUIRE_ISSUE_PHOTO,
        "cluster": {"radius_km": CLUSTER_RADIUS_KM, "window_hours": CLUSTER_TIME_WINDOW_HOURS,
                    "min_reports": MIN_CLUSTER_SIZE},
        "asset_match_radius_m": ASSET_MATCH_RADIUS_M,
    }


# No endpoint lists accounts or credentials. Demo logins are handed to presenters out of band
# (data/demo/demo_credentials.txt is written locally by seed_demo.py and never served).
