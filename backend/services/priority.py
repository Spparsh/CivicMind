"""
CivicMind — Priority Engine (explainable, bounded 0-100).

A transparent ADDITIVE model — every point is attributable to one factor:

    factor                         max points   basis
    ---------------------------------------------------------------------
    reported severity (1-5)            35       (severity-1)/4 x 35
    number of linked reports           20       log-scaled; 1 report = 0, 10+ = 20
    time unresolved                    15       linear to 7 days since first report
    category public-safety weight      15       configured weight per category
    critical infrastructure link       15       matched asset criticality
                                      ---
                                      100

Labels use the thresholds in data/civicmind_config.json.
This is a prototype triage policy, not an official municipal standard. It
does not estimate affected population (no population data is available).
"""
import math

from config import (PRIORITY_CRITICAL_THRESHOLD, PRIORITY_HIGH_THRESHOLD,
                    PRIORITY_MEDIUM_THRESHOLD)
from database import parse_ts, utcnow

WEIGHTS = {"severity": 35, "reports": 20, "age": 15, "category": 15, "asset": 15}
AGE_CAP_HOURS = 24 * 7

# Relative public-safety weight per category (0..1). Prototype policy.
CATEGORY_RISK = {
    "Sewage Overflow": 1.0, "Water Leakage": 0.8, "Traffic Signal Failure": 0.8,
    "Drainage Issue": 0.75, "Fallen Tree": 0.7, "Pothole": 0.6,
    "Low Water Pressure": 0.6, "Road Damage": 0.5, "Streetlight Failure": 0.45,
    "Garbage Accumulation": 0.4, "Air/Noise Pollution": 0.35, "Encroachment": 0.1,
    "Other": 0.0,
}
ASSET_CRITICALITY_POINTS = {"critical": 1.0, "high": 0.6, "medium": 0.25, "low": 0.0}


def label_for(score):
    if score >= PRIORITY_CRITICAL_THRESHOLD:
        return "Critical"
    if score >= PRIORITY_HIGH_THRESHOLD:
        return "High"
    if score >= PRIORITY_MEDIUM_THRESHOLD:
        return "Medium"
    return "Low"


def compute_priority(severity, report_count, first_report_at, category,
                     asset_code=None, asset_criticality=None, now=None):
    severity = max(1, min(5, int(severity or 1)))
    report_count = max(1, int(report_count or 1))
    now = now or utcnow()
    first = parse_ts(first_report_at) or now
    age_h = max(0.0, (now - first).total_seconds() / 3600.0)

    sev_pts = (severity - 1) / 4 * WEIGHTS["severity"]
    rep_pts = min(1.0, math.log10(report_count)) * WEIGHTS["reports"]
    age_pts = min(1.0, age_h / AGE_CAP_HOURS) * WEIGHTS["age"]
    cat_w = CATEGORY_RISK.get(category, 0.0)
    cat_pts = cat_w * WEIGHTS["category"]
    asset_w = ASSET_CRITICALITY_POINTS.get((asset_criticality or "").lower(), 0.0) if asset_code else 0.0
    asset_pts = asset_w * WEIGHTS["asset"]

    factors = [
        {"key": "severity", "label": "Reported severity", "points": round(sev_pts, 1),
         "max": WEIGHTS["severity"], "detail": f"{severity}/5"},
        {"key": "reports", "label": "Linked reports", "points": round(rep_pts, 1),
         "max": WEIGHTS["reports"], "detail": f"{report_count} report{'s' if report_count != 1 else ''}"},
        {"key": "age", "label": "Time unresolved", "points": round(age_pts, 1),
         "max": WEIGHTS["age"], "detail": _age_text(age_h)},
        {"key": "category", "label": "Category safety weight", "points": round(cat_pts, 1),
         "max": WEIGHTS["category"], "detail": f"{category} ({cat_w:.2f})"},
        {"key": "asset", "label": "Critical infrastructure", "points": round(asset_pts, 1),
         "max": WEIGHTS["asset"],
         "detail": f"{asset_code} — {asset_criticality}" if asset_code else "No matched asset"},
    ]
    score = round(min(100.0, sum(f["points"] for f in factors)), 1)
    label = label_for(score)
    return {
        "priority_score": score,
        "priority_label": label,
        "factors": factors,
        "reasons": top_reasons(factors),
        "reason_text": reason_text(score, label, factors),
        "model": "additive-v1",
    }


def _age_text(hours):
    if hours < 1:
        return "under 1 hour"
    if hours < 48:
        return f"{hours:.0f} hours"
    return f"{hours / 24:.1f} days"


_TIE_ORDER = {"asset": 0, "severity": 1, "reports": 2, "age": 3, "category": 4}


def top_reasons(factors, n=4):
    """Human-readable main drivers (only factors that actually contributed)."""
    ranked = sorted((f for f in factors if f["points"] >= 0.25 * f["max"]),
                    key=lambda f: (-f["points"] / f["max"], _TIE_ORDER[f["key"]]))
    phrases = {
        "severity": lambda f: f"High reported severity ({f['detail']})" if f["points"] >= 0.75 * f["max"]
                    else f"Reported severity {f['detail']}",
        "reports": lambda f: f"Multiple matching reports ({f['detail']})",
        "age": lambda f: f"Unresolved for {f['detail']}",
        "category": lambda f: f"Safety-relevant category ({f['detail'].split(' (')[0]})",
        "asset": lambda f: f"Critical asset linked ({f['detail']})",
    }
    return [phrases[f["key"]](f) for f in ranked[:n]]


def reason_text(score, label, factors):
    reasons = top_reasons(factors)
    return f"{label} priority (score {score}/100)" + (": " + "; ".join(reasons) if reasons else ".")
