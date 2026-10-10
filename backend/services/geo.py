"""
CivicMind — geospatial helpers and area (ward/zone) mapping.

IMPORTANT LIMITATION: ward assignment is an APPROXIMATION. No authoritative
ward boundary polygons are bundled, so a point is assigned to the ward whose
configured *centre point* is nearest (data/civicmind_config.json -> wards).
Results are labelled `method: "nearest_ward_centre"` so the UI never presents
this as polygon-based GIS analysis.
"""
import math

from config import WARDS, CLUSTER_RADIUS_KM, CITY_CENTER, SERVICE_RADIUS_KM

WARD_METHOD = "nearest_ward_centre"


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    """Great-circle distance in kilometres."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def valid_coordinates(lat, lng) -> bool:
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180


def in_service_area(lat, lng) -> bool:
    """True when the point lies within the configured radius of the city centre."""
    if not valid_coordinates(lat, lng):
        return False
    return haversine_km(lat, lng, CITY_CENTER["lat"], CITY_CENTER["lng"]) <= SERVICE_RADIUS_KM


def nearest_ward(lat, lng):
    """Return (ward_name, zone) for the closest configured ward centre, or (None, None)."""
    if not WARDS:
        return None, None
    best, best_d = (None, None), 1e9
    for name, (wlat, wlng, zone) in WARDS.items():
        d = haversine_km(lat, lng, wlat, wlng)
        if d < best_d:
            best, best_d = (name, zone), d
    return best


def map_area(lat, lng):
    """Public area-mapping helper -> {ward, zone, distance_km, method}."""
    ward, zone = nearest_ward(lat, lng)
    if not ward:
        return {"ward": None, "zone": None, "distance_km": None, "method": WARD_METHOD}
    d = haversine_km(lat, lng, WARDS[ward][0], WARDS[ward][1])
    return {"ward": ward, "zone": zone, "distance_km": round(d, 2), "method": WARD_METHOD}


def centroid(points):
    """Average lat/lng of a list of (lat, lng) tuples (fine at city scale)."""
    if not points:
        return (0.0, 0.0)
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def spread_km(points):
    """Max distance from the centroid, in km (approximate geographic spread)."""
    if len(points) < 2:
        return 0.0
    clat, clng = centroid(points)
    return round(max(haversine_km(clat, clng, p[0], p[1]) for p in points), 2)


def within_radius(a, b, radius_km=CLUSTER_RADIUS_KM) -> bool:
    return haversine_km(a[0], a[1], b[0], b[1]) <= radius_km


def generalise(lat, lng, places=3):
    """Round coordinates for public/low-privilege views (3 dp ≈ 110 m)."""
    return round(float(lat), places), round(float(lng), places)
