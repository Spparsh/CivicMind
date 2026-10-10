"""
CivicMind — reverse geocoding for "Use my current location" and address search
(server-side, opt-in).

Disabled unless CIVICMIND_REVERSE_GEOCODER=nominatim. When enabled it follows
the public Nominatim usage policy (https://operations.osmfoundation.org/policies/nominatim/):
one lookup per explicit user click, at most 1 request per second for the
whole application, an identifying User-Agent, a result cache, attribution,
and a single switch to turn it off. Coordinates outside the service area are
never sent. The result is a suggestion the citizen can edit; the map pin
remains the authoritative location.
"""
import math
import re
import threading
import time

import requests

import config
from services import geo

ATTRIBUTION = "Address © OpenStreetMap contributors, via Nominatim (ODbL)"
_cache, _lock, _last = {}, threading.Lock(), [0.0]


def available():
    return config.REVERSE_GEOCODER == "nominatim"


def _landmark(data):
    a = data.get("address") if isinstance(data.get("address"), dict) else {}
    name = data.get("name") if isinstance(data.get("name"), str) else ""
    parts = [name] if name else []
    for k in ("road", "neighbourhood", "suburb"):
        v = a.get(k)
        if isinstance(v, str) and v and v not in parts:
            parts.append(v)
    return ", ".join(parts[:3])[:200] or None


def _get(url, params):
    """One provider request, spaced ≥ 1.05 s from the previous one (caller holds _lock)."""
    wait = 1.05 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    try:
        resp = requests.get(url, timeout=6, allow_redirects=False, params=params,
                            headers={"User-Agent": config.REVERSE_GEOCODER_USER_AGENT})
        return resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def _viewbox():
    """Bounding box of the service area (left, top, right, bottom) for bounded searches."""
    lat, lng = config.CITY_CENTER["lat"], config.CITY_CENTER["lng"]
    dlat = config.SERVICE_RADIUS_KM / 111.0
    dlng = config.SERVICE_RADIUS_KM / (111.0 * max(0.2, math.cos(math.radians(lat))))
    return f"{lng - dlng:.5f},{lat + dlat:.5f},{lng + dlng:.5f},{lat - dlat:.5f}"


def _norm(s):
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


def search(q, lang="en"):
    """Find places for an address/landmark typed by the citizen. Never raises.

    Local ward names (from the city configuration) are always searched, offline.
    The public geocoder is asked only when enabled, only on an explicit search
    (never as-you-type — the Nominatim policy forbids autocomplete), bounded to
    the service area, cached, and spaced ≥ 1 s apart application-wide.
    """
    q = " ".join((q or "").split())[:120]
    if len(q) < 3:
        return {"results": [], "provider": None, "message": "Type at least 3 characters."}
    nq = _norm(q)
    results = []
    for name, (wlat, wlng, zone) in config.WARDS.items():
        if nq and (nq in _norm(name) or _norm(name) in nq):
            results.append({"label": f"{name} ({zone}) — ward centre", "lat": wlat, "lng": wlng,
                            "source": "ward_centre", "approximate": True})
    provider = None
    if available():
        provider = "nominatim"
        key = ("s", nq, lang)
        with _lock:
            data = _cache.get(key)
            if data is None:
                data = _get(config.GEOCODER_SEARCH_URL,
                            {"q": q, "format": "jsonv2", "limit": 6, "countrycodes": "in", "bounded": 1,
                             "viewbox": _viewbox(), "addressdetails": 0,
                             "accept-language": lang if lang in ("en", "hi", "mr") else "en"})
                if isinstance(data, list):
                    if len(_cache) > 2000:
                        _cache.clear()
                    _cache[key] = data
        for item in data if isinstance(data, list) else []:
            try:
                lat, lng = float(item["lat"]), float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            label = item.get("display_name")
            if not isinstance(label, str) or not geo.in_service_area(lat, lng):
                continue
            results.append({"label": " ".join(label.split())[:300], "lat": lat, "lng": lng,
                            "source": "nominatim", "approximate": False})
    out = {"results": results[:8], "provider": provider,
           "attribution": ATTRIBUTION if provider else None}
    if not results:
        out["message"] = ("No matching place found in the service area. Try a nearby landmark or set the pin on the map."
                          if provider else
                          "Address search is not enabled on this server; only ward names can be found. "
                          "Set the pin on the map or use your current location.")
    return out


def reverse(lat, lng):
    """Return {available, address, landmark, attribution} — never raises."""
    if not available():
        return {"available": False, "reason": "not_configured",
                "message": "Address lookup is not enabled on this server. Please type a landmark or address."}
    if not geo.valid_coordinates(lat, lng) or not geo.in_service_area(lat, lng):
        return {"available": False, "reason": "outside_service_area",
                "message": "That location is outside the service area."}
    key = (round(lat, 4), round(lng, 4))
    with _lock:
        if key in _cache:
            return dict(_cache[key], cached=True)
        data = _get(config.REVERSE_GEOCODER_URL,
                    {"lat": f"{lat:.6f}", "lon": f"{lng:.6f}", "format": "jsonv2",
                     "zoom": 18, "addressdetails": 1, "accept-language": "en"})
        if not isinstance(data, dict) or not isinstance(data.get("display_name"), str):
            return {"available": False, "reason": "lookup_failed",
                    "message": "Could not find an address for this point. Please type a landmark."}
        out = {"available": True, "address": " ".join(data["display_name"].split())[:300],
               "landmark": _landmark(data), "attribution": ATTRIBUTION, "provider": "nominatim"}
        if len(_cache) > 2000:
            _cache.clear()
        _cache[key] = out
        return out
