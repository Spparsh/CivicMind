"""CivicMind — small, dependency-free geometry helpers (EPSG:4326, GeoJSON lng/lat order).

Distances use a local equirectangular projection, accurate to well under a
metre at city scale. Spatial proximity is used only to propose CANDIDATE
assets for an incident — never to create a network connection."""
import json
import math

MAX_VERTICES = 2000
_M_PER_DEG_LAT = 111_320.0


class GeometryError(ValueError):
    pass


def _point(c):
    if not isinstance(c, (list, tuple)) or len(c) < 2:
        raise GeometryError("A coordinate must be [longitude, latitude].")
    lng, lat = c[0], c[1]
    if isinstance(lng, bool) or isinstance(lat, bool) or not all(isinstance(v, (int, float)) for v in (lng, lat)):
        raise GeometryError("Coordinates must be numbers.")
    if not (math.isfinite(lng) and math.isfinite(lat)):
        raise GeometryError("Coordinates must be finite numbers.")
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise GeometryError("Coordinates are out of range (GeoJSON order is [longitude, latitude]).")
    return [round(float(lng), 7), round(float(lat), 7)]


def validate(geom):
    """Return (geometry_type, normalized geometry dict, bbox (min_lat, min_lng, max_lat, max_lng))."""
    if not isinstance(geom, dict):
        raise GeometryError("Missing geometry.")
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point":
        pts = [_point(coords)]
    elif gtype == "LineString":
        if not isinstance(coords, list) or len(coords) < 2:
            raise GeometryError("A LineString needs at least two positions.")
        if len(coords) > MAX_VERTICES:
            raise GeometryError(f"A LineString may have at most {MAX_VERTICES} positions.")
        pts = [_point(c) for c in coords]
        if all(p == pts[0] for p in pts):
            raise GeometryError("A LineString cannot have zero length.")
    else:
        raise GeometryError("Only Point and LineString geometries are supported.")
    lats, lngs = [p[1] for p in pts], [p[0] for p in pts]
    norm = {"type": gtype, "coordinates": pts[0] if gtype == "Point" else pts}
    return gtype, norm, (min(lats), min(lngs), max(lats), max(lngs))


def coords_latlng(geometry):
    g = json.loads(geometry) if isinstance(geometry, str) else geometry
    c = g["coordinates"]
    return [(c[1], c[0])] if g["type"] == "Point" else [(p[1], p[0]) for p in c]


def _xy(lat, lng, lat0):
    return lng * _M_PER_DEG_LAT * math.cos(math.radians(lat0)), lat * _M_PER_DEG_LAT


def distance_m(lat, lng, geometry):
    """Shortest distance in metres from a point to a Point/LineString geometry."""
    pts = coords_latlng(geometry)
    px, py = _xy(lat, lng, lat)
    best = math.inf
    if len(pts) == 1:
        x, y = _xy(*pts[0], lat)
        return math.hypot(px - x, py - y)
    for (a_lat, a_lng), (b_lat, b_lng) in zip(pts, pts[1:]):
        ax, ay = _xy(a_lat, a_lng, lat)
        bx, by = _xy(b_lat, b_lng, lat)
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best


def length_m(geometry):
    pts = coords_latlng(geometry)
    if len(pts) < 2:
        return 0.0
    lat0 = pts[0][0]
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        ax, ay = _xy(*a, lat0)
        bx, by = _xy(*b, lat0)
        total += math.hypot(bx - ax, by - ay)
    return total


def representative_point(geometry):
    """A point ON the geometry (middle vertex for lines) for labels and map focus."""
    pts = coords_latlng(geometry)
    return pts[len(pts) // 2] if len(pts) > 2 else (
        pts[0] if len(pts) == 1 else ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2))


def degree_margin(radius_m, lat):
    return radius_m / _M_PER_DEG_LAT, radius_m / (_M_PER_DEG_LAT * max(0.1, math.cos(math.radians(lat))))
