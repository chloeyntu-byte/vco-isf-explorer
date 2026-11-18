"""Secondary comparison of deterministic HBAC and noise-modulation phase axes."""

import math

PHASE_AXIS_COMPARISON = "phase_axis_mod_180"


def hbac_axis_delta(signed_delta_deg):
    """Distance to the nearer of the parallel and antiparallel phase directions."""
    try:
        delta = float(signed_delta_deg)
    except (TypeError, ValueError):
        return None
    return abs(math.remainder(delta, 180.0)) if math.isfinite(delta) else None


def hbac_phase_check(signed_deltas, threshold):
    """Summarize phase-axis agreement over nonzero orders with defined phases."""
    deltas = map(hbac_axis_delta, signed_deltas)
    maximum = max((delta for delta in deltas if delta is not None), default=None)
    passed = maximum <= threshold if maximum is not None else None
    return {
        "hbac_phase_comparison": PHASE_AXIS_COMPARISON,
        "hbac_max_phase_delta_deg": maximum,
        "hbac_phase_warn_threshold_deg": threshold,
        "hbac_secondary_validation_pass": passed,
        "hbac_secondary_validation_code": "ISF-W3003" if passed is False else "",
    }
