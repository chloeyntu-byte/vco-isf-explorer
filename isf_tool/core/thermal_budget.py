"""Portable thermal evidence and recomputed display quantities (no fitting)."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping
from pathlib import Path

from ..calculation import dBc_from_pm_asd


def _power(value, label):
    if isinstance(value, bool):
        raise ValueError(label + " must be nonnegative finite PSD")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(label + " must be nonnegative finite PSD") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(label + " must be nonnegative finite PSD")
    return value


def thermal_evidence(row):
    value = row.get("thermal_noise", row.get("thermal_noise_json"))
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if value is None:
        return None
    if not isinstance(value, Mapping) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported thermal evidence schema")
    output = copy.deepcopy(dict(value))
    if output.get("status") == "unavailable":
        return output
    if output.get("status") != "available" or output.get("noise_quantity") != "PM_SSB" or not output.get("pm_validation"):
        raise ValueError("thermal result needs independent PM-SSB validation")
    if output.get("scope") in {"gm_pair_and_passive_network", "gm_pair_passives_and_selected_objects"}:
        return {"schema_version": 1, "status": "unavailable", "previous_scope": output.get("scope"),
                "reason": "legacy thermal results may include capacitor-bank internal noise; rerun gm + LC measurement (rerun required)"}
    if output.get("scope") != "gm_pair_and_lc_network" or output.get("pair_model") != "symmetric_independent":
        raise ValueError("unsupported thermal source scope")
    if output.get("passive_scope") != "passive_only_lc_roots_v1":
        return {"schema_version": 1, "status": "unavailable",
                "reason": "LC ownership was not verified; rerun gm + LC measurements"}
    seen = set()
    band_limit = output.get("input_max_harmonic")
    if band_limit is not None and (type(band_limit) is not int or band_limit < 0):
        raise ValueError("invalid thermal input harmonic limit")
    if band_limit is not None and (type(output.get("requested_max_harmonic")) is not int
                                   or output["requested_max_harmonic"] != band_limit):
        raise ValueError("thermal band limit differs from requested calculation order")
    sources = output.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("thermal evidence requires source contributions")
    for source in sources:
        if not isinstance(source, Mapping):
            raise ValueError("thermal source must be an object")
        key = source.get("device"), source.get("mechanism")
        if not all(isinstance(x, str) and x for x in key) or key in seen:
            raise ValueError("invalid/duplicate thermal source")
        seen.add(key)
        if source.get("object_id") or source.get("source_basis") == "paired_noise_TB_transient_state_white_PSD":
            return {"schema_version": 1, "status": "unavailable",
                    "reason": "retired additional-object sources found; rerun gm + LC measurements"}
        if source.get("source_basis") == "VCO_static_intrinsic_PSD_at_actual_input_bands":
            roots = output.get("passive_roots")
            if not isinstance(roots, list) or source["device"].split(".", 1)[0] not in roots:
                raise ValueError("thermal passive source has no verified LC root owner")
        power = _power(source.get("psd_V2_Hz"), "source")
        source["psd_V2_Hz"] = power
        parts = source.get("input_band_contributions")
        if not isinstance(parts, list) or not parts:
            raise ValueError("thermal source has no independent input-band powers")
        if any(not isinstance(part, Mapping) for part in parts):
            raise ValueError("thermal input band must be an object")
        bands = [part.get("input_band") for part in parts]
        if any(type(k) is not int for k in bands) or len(bands) != len(set(bands)):
            raise ValueError("invalid/duplicate thermal input band")
        if band_limit is not None and any(abs(k) > band_limit for k in bands):
            raise ValueError("thermal contribution exceeds requested harmonic limit")
        accumulated = math.fsum(_power(part.get("psd_V2_Hz"), "input band") for part in parts)
        for part in parts:
            part["psd_V2_Hz"] = _power(part["psd_V2_Hz"], "input band")
        if not math.isclose(power, accumulated, rel_tol=1e-10, abs_tol=1e-40):
            raise ValueError("thermal source does not equal independent band power sum")
    total = math.fsum(_power(s["psd_V2_Hz"], "source") for s in sources)
    if not math.isclose(total, _power(output.get("predicted_psd_V2_Hz"), "thermal total"), rel_tol=1e-10, abs_tol=1e-40):
        raise ValueError("thermal total does not equal source power sum")
    carrier = _power(output.get("carrier_peak_V"), "carrier")
    output["carrier_peak_V"] = carrier
    output["predicted_psd_V2_Hz"] = total
    if carrier <= 0:
        raise ValueError("thermal carrier must be positive")
    if row.get("carrier_peak_V") not in (None, "") and not math.isclose(float(row["carrier_peak_V"]), carrier, rel_tol=1e-4):
        raise ValueError("thermal/flicker carrier mismatch")
    offset = row.get("offset_Hz", row.get("offset_hz"))
    budget_offset = _power(output.get("offset_Hz"), "thermal offset")
    f0 = _power(output.get("f0_Hz"), "thermal carrier frequency")
    if not 0 < budget_offset < f0:
        raise ValueError("thermal offset must be positive and below carrier frequency")
    output["offset_Hz"], output["f0_Hz"] = budget_offset, f0
    if offset not in (None, "") and float(offset) != budget_offset:
        raise ValueError("thermal evidence belongs to a different offset")
    if row.get("f0_Hz") not in (None, ""):
        row_f0 = float(row["f0_Hz"])
        # The unchanged flicker reader uses psf's six-significant-digit dump.
        # Accept its exact serialization, not an arbitrary broad tolerance.
        rounded_f0 = float(format(f0, ".6g"))
        if not (math.isclose(row_f0, f0, rel_tol=1e-10) or
                math.isclose(row_f0, rounded_f0, rel_tol=1e-12)):
            raise ValueError("thermal/flicker carrier frequency mismatch")
    output["phase_noise_dBc_Hz"] = dBc_from_pm_asd(math.sqrt(total), carrier)
    for source in sources:
        source["phase_noise_dBc_Hz"] = dBc_from_pm_asd(math.sqrt(source["psd_V2_Hz"]), carrier)
    return output


def load_thermal_evidence(cfg, case_dir, summary):
    """Never resurrect an old budget when analyze-only explicitly disables it."""
    if cfg.get("thermal_enabled", True) is False:
        return {"schema_version": 1, "status": "unavailable",
                "reason": "thermal disabled in expert configuration"}
    path = Path(case_dir) / "thermal" / "budget.json"
    if not path.is_file():
        return None
    evidence = thermal_evidence(dict(summary, thermal_noise=json.loads(path.read_text(encoding="utf-8"))))
    if evidence and evidence.get("status") == "available" and evidence.get("input_max_harmonic") is not None:
        recorded = evidence["input_max_harmonic"]
        if cfg.get("max_harmonic") is not None and cfg["max_harmonic"] != recorded:
            return {"schema_version": 1, "status": "unavailable",
                    "reason": "thermal harmonic limit changed (saved N={}, requested N={}); rerun thermal measurements".format(
                        recorded, cfg["max_harmonic"])}
    return evidence


def calculated_noise_dbc(row, *, noise_kind="both", scope="pair"):
    if noise_kind not in {"flicker", "thermal", "both"}:
        raise ValueError("noise_kind must be flicker, thermal or both")
    if noise_kind == "flicker":
        return row.get("predicted_pair_dBc_Hz" if scope == "pair" else "dynamic_dBc_Hz")
    thermal = thermal_evidence(row)
    if not thermal or thermal.get("status") != "available":
        return None
    if scope != "pair":
        return None  # No fictitious single-device LC/pair-total view.
    if noise_kind == "thermal":
        return thermal["phase_noise_dBc_Hz"]
    flicker = row.get("predicted_pair_pm_asd_V_rtHz", row.get("predicted_pair_fn_pm_asd_V_rtHz"))
    if flicker is None:
        return None
    power = _power(flicker, "pair flicker ASD") ** 2 + thermal["predicted_psd_V2_Hz"]
    return dBc_from_pm_asd(math.sqrt(power), thermal["carrier_peak_V"])
