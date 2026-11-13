"""Result-owned units, aliases and plotting series; no simulation or model fitting.

Each sample is identified by (operating_point_id, offset_Hz).  A curve never
joins different operating points, and missing samples are not interpolated.
The main comparison uses the whole oscillator's simulated phase noise.
Device flicker comparisons remain separate; the harmonic total is coherent.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping

from ..calculation import dBc_from_pm_asd, error_dB
from .thermal_budget import calculated_noise_dbc, thermal_evidence


SUMMARY_QUALITY_FIELDS = (
    "pm_magnitude_projection_valid", "pm_magnitude_projection_code",
    "pm_magnitude_projection_threshold_dB", "spectre_am_to_pm_dB",
    "spectre_fn_am_asd_V_rtHz", "predicted_carrier_projection_loss_dB",
    "numerical_quality_pass", "numerical_quality_code", "numerical_quality_issues",
    "hbac_secondary_validation_pass", "hbac_secondary_validation_code",
    "hbac_max_phase_delta_deg", "hbac_phase_warn_threshold_deg",
)
_BOOL_QUALITY = {
    "pm_magnitude_projection_valid", "numerical_quality_pass",
    "hbac_secondary_validation_pass",
}


def _number(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def _boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() in {"true", "1"}:
            return True
        if value.strip().lower() in {"false", "0"}:
            return False
    return None


def _value(row, *keys):
    for key in keys:
        if key in row:
            value = _number(row[key])
            if value is not None:
                return value
    return None


def quality_fields(row):
    """Keep per-sample quality evidence; absent checks remain unknown, not PASS."""
    output = {}
    for key in SUMMARY_QUALITY_FIELDS:
        if key in _BOOL_QUALITY:
            output[key] = _boolean(row.get(key))
        elif key in row:
            value = row[key]
            output[key] = (_number(value) if key.endswith(("_dB", "_deg", "_rtHz")) else value)
    return output


def enrich_summary(row, *, offset_hz=None, pair_model=None):
    """Add carrier-normalized display quantities using the existing unit contract."""
    output = copy.deepcopy(dict(row))
    reference = row.get("spectre_noise_reference")
    total_owned = (isinstance(reference, Mapping)
                   and reference.get("scope") == "whole_vco"
                   and type(reference.get("schema_version")) is int
                   and reference.get("schema_version") == 1)
    # Historical engine CSVs used this ASD name for one device's total.
    # Only the explicit new provenance contract establishes whole-VCO scope.
    if not total_owned:
        output["spectre_total_pm_asd_V_rtHz"] = None
        output["spectre_total_dBc_Hz"] = None
        output["spectre_noise_contributions"] = []
    output["operating_point_id"] = str(row.get("operating_point_id") or row.get("point") or "")
    offset = _value(row, "offset_Hz", "offset_hz")
    if offset is None:
        offset = _number(offset_hz)
    output["offset_Hz"] = output["offset_hz"] = offset
    carrier = _value(row, "carrier_peak_V")
    for key, aliases in (
        ("predicted_pair_pm_asd_V_rtHz", ("predicted_pair_pm_asd_V_rtHz", "predicted_pair_fn_pm_asd_V_rtHz")),
        ("spectre_pair_pm_asd_V_rtHz", ("spectre_pair_pm_asd_V_rtHz", "spectre_pair_fn_pm_asd_V_rtHz")),
    ):
        output[key] = _value(row, *aliases)
    for db_key, asd_key in (
        ("dc_dBc_Hz", "dc_pm_asd_V_rtHz"),
        ("delay_dBc_Hz", "delay_pm_asd_V_rtHz"),
        ("dynamic_dBc_Hz", "dynamic_pm_asd_V_rtHz"),
        ("spectre_fn_dBc_Hz", "spectre_fn_pm_asd_V_rtHz"),
        ("spectre_total_dBc_Hz", "spectre_total_pm_asd_V_rtHz"),
        ("predicted_pair_dBc_Hz", "predicted_pair_pm_asd_V_rtHz"),
        ("spectre_pair_dBc_Hz", "spectre_pair_pm_asd_V_rtHz"),
    ):
        asd = _value(output, asd_key)
        output[db_key] = (dBc_from_pm_asd(asd, carrier)
                          if asd is not None and carrier is not None else _number(output.get(db_key)))
    output.update(quality_fields(row))
    output["noise_scope"] = "target_device_flicker"
    output["pair_noise_scope"] = "independent_device_pair_flicker"
    output["pair_model"] = row.get("pair_model") or pair_model
    # Retain explicit flicker pair identity before legacy thermal qualification
    # drops its payload. This is display membership, never thermal eligibility
    # or an invented per-device prediction. Repeated enrichment is lossless.
    identity = row.get("flicker_pair_identity") or row.get("thermal_noise") or {}
    output.pop("flicker_pair_identity", None)
    if isinstance(identity, Mapping) and output["pair_model"] == "symmetric_independent":
        target, peer = identity.get("target_noise_trace"), identity.get("peer_noise_trace")
        if (identity.get("pair_model") == "symmetric_independent"
                and all(isinstance(v, str) and v.strip() for v in (target, peer)) and target != peer):
            output["flicker_pair_identity"] = dict(target_noise_trace=target,
                peer_noise_trace=peer, pair_model="symmetric_independent")
    output["thermal_noise"] = thermal_evidence(output)
    output["thermal_dBc_Hz"] = calculated_noise_dbc(output, noise_kind="thermal")
    output["predicted_total_dBc_Hz"] = calculated_noise_dbc(output, noise_kind="both")
    return output


def _complex_aliases(row, names):
    values = [_number(row.get(name)) for name in names]
    present = [value for value in values if value is not None]
    if present and any(not math.isclose(value, present[0], rel_tol=1e-12, abs_tol=1e-30) for value in present[1:]):
        raise ValueError("conflicting result aliases: " + ", ".join(names))
    return present[0] if present else None


def enrich_harmonic(row, summary):
    """Add equivalent single-order/cumulative noise, never an incoherent sum."""
    output = copy.deepcopy(dict(row))
    for canonical, alias in (("Q_real_V_rtHz", "Qk_real_V_rtHz"), ("Q_imag_V_rtHz", "Qk_imag_V_rtHz")):
        output[canonical] = output[alias] = _complex_aliases(row, (canonical, alias))
    real, imag = output["Q_real_V_rtHz"], output["Q_imag_V_rtHz"]
    magnitude = math.hypot(real, imag) if real is not None and imag is not None else _value(row, "Q_mag_V_rtHz", "Qk_mag_V_rtHz")
    output["Q_mag_V_rtHz"] = magnitude
    if real is not None and imag is not None:
        output["Q_phase_deg"] = math.degrees(math.atan2(imag, real)) if magnitude else 0.0
    cumulative_real = _value(row, "cum_real_V_rtHz", "cum_real")
    cumulative_imag = _value(row, "cum_imag_V_rtHz", "cum_imag")
    cumulative = (math.hypot(cumulative_real, cumulative_imag)
                  if cumulative_real is not None and cumulative_imag is not None else _value(row, "cum_mag_V_rtHz"))
    output["cum_mag_V_rtHz"] = cumulative
    carrier = _value(summary, "carrier_peak_V")
    reference = _value(summary, "spectre_fn_pm_asd_V_rtHz")
    output["harmonic_equivalent_dBc_Hz"] = dBc_from_pm_asd(magnitude, carrier) if magnitude is not None and carrier is not None else None
    output["cumulative_dBc_Hz"] = dBc_from_pm_asd(cumulative, carrier) if cumulative is not None and carrier is not None else None
    output["cumulative_error_dB"] = error_dB(cumulative, reference) if cumulative is not None and reference is not None else None
    pair_reference = _value(summary, "spectre_pair_pm_asd_V_rtHz", "spectre_pair_fn_pm_asd_V_rtHz")
    output["pair_harmonic_equivalent_dBc_Hz"] = dBc_from_pm_asd(math.sqrt(2) * magnitude, carrier) if magnitude is not None and carrier is not None else None
    output["pair_cumulative_dBc_Hz"] = dBc_from_pm_asd(math.sqrt(2) * cumulative, carrier) if cumulative is not None and carrier is not None else None
    output["pair_cumulative_error_dB"] = error_dB(math.sqrt(2) * cumulative, pair_reference) if cumulative is not None and pair_reference is not None else None
    for key in ("point", "operating_point_id", "offset_Hz", "offset_hz", "corner"):
        output[key] = summary.get(key)
    output["noise_scope"] = "target_device_flicker"
    output["harmonic_combination"] = "coherent_complex_sum"
    output.update(quality_fields(summary))
    return output


def _unwrap(result):
    if not isinstance(result, Mapping):
        return {}
    for key in ("result", "data"):
        if isinstance(result.get(key), Mapping):
            return result[key]
    return result


def _result_metadata(source):
    dual_tb = source.get("dual_tb")
    metadata = dual_tb.get("metadata") if isinstance(dual_tb, Mapping) else None
    if not isinstance(metadata, Mapping) or not metadata:
        metadata = source.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def is_generated_flicker(result):
    source = _unwrap(result)
    if _result_metadata(source).get("replica_source") == "generated":
        return True
    rows = source.get("summary") or []
    return bool(rows) and all(isinstance(row, Mapping) and row.get("replica_source") == "generated" for row in rows)


def enrich_result(result):
    """Return a copy with display fields and identities on every result row."""
    source = _unwrap(result)
    output = copy.deepcopy(dict(source))
    metadata = _result_metadata(source)
    owned_offset = _value(source, "offset_Hz", "offset_hz")
    if owned_offset is None:
        owned_offset = _value(metadata, "offset_Hz", "offset_hz")
    pair_model = metadata.get("second_device_model")
    generated = metadata.get("replica_source") == "generated"
    output["summary"] = []
    for row in source.get("summary", []):
        summary = enrich_summary(row, offset_hz=owned_offset, pair_model=pair_model)
        if generated:
            summary.update(replica_source="generated", target_noise_trace=summary.get("target_noise_trace") or metadata.get("target_noise_trace"))
        output["summary"].append(summary)
    summaries_by_point = {}
    for row in output["summary"]:
        summaries_by_point.setdefault(str(row.get("point") or ""), []).append(row)
    for point, tables in output.get("points", {}).items():
        point_summaries = summaries_by_point.get(str(point), [])
        if not point_summaries:
            point_summaries = [enrich_summary(dict(row, point=point), offset_hz=owned_offset, pair_model=pair_model) for row in tables.get("summary", [])]
        if not point_summaries:
            continue
        for table_name in ("harmonics", "workpoint", "phase_candidates", "summary"):
            enriched_rows = []
            for row in tables.get(table_name, []):
                offset = _value(row, "offset_Hz", "offset_hz")
                candidates = [summary for summary in point_summaries if offset is None or summary["offset_Hz"] == offset]
                if len(candidates) != 1:
                    raise ValueError("ambiguous result sample for point " + str(point))
                summary = candidates[0]
                if table_name == "harmonics":
                    item = enrich_harmonic(row, summary)
                elif table_name == "summary":
                    item = dict(row, **summary)
                else:
                    item = dict(row)
                    for key in ("point", "operating_point_id", "offset_Hz", "offset_hz", "corner"):
                        item[key] = summary.get(key)
                enriched_rows.append(item)
            tables[table_name] = enriched_rows
    return output


def _grouped_samples(result):
    source = enrich_result(result)
    groups = {}
    for row in source.get("summary", []):
        identity = row["operating_point_id"]
        group = groups.setdefault(identity, [])
        if any(item["offset_Hz"] == row["offset_Hz"] for item in group):
            raise ValueError("duplicate operating point / offset sample: " + identity)
        if group and str(group[0].get("corner") or "") != str(row.get("corner") or ""):
            raise ValueError("one operating_point_id refers to different corners: " + identity)
        group.append(row)
    for group in groups.values():
        group.sort(key=lambda row: row["offset_Hz"] if row["offset_Hz"] is not None else math.inf)
    return source, groups


def operating_points(result):
    """Return result-owned identities/parameters without parsing point names."""
    _source, groups = _grouped_samples(result)
    return [{"id": identity, "label": identity, "corner": rows[0].get("corner"),
             "parameters": copy.deepcopy(rows[0].get("operating_parameters") or {}),
             "offsets_hz": [row["offset_Hz"] for row in rows if row["offset_Hz"] is not None],
             "points": [str(row.get("point") or "") for row in rows]}
            for identity, rows in sorted(groups.items())]


def _select_group(groups, identity):
    if identity is None:
        identity = sorted(groups)[0] if groups else None
    return [row for row in groups.get(str(identity), []) if row["offset_Hz"] is not None and row["offset_Hz"] > 0]


def _series(identity, name, rows, values, *, scope="single", harmonic=None):
    samples = [{"point": row.get("point"), "operating_point_id": row["operating_point_id"],
                "offset_hz": row["offset_Hz"], "value": value, "quality": quality_fields(row)}
               for row, value in zip(rows, values)]
    return {"id": identity, "name": name, "scope": scope, "unit": "dBc/Hz", "k": harmonic,
            "points": [[sample["offset_hz"], sample["value"]] for sample in samples], "samples": samples}


def phase_noise_series(result, operating_point_id=None, scope="single", noise_kind="flicker"):
    """ISF device flicker versus measured total VCO PM-SSB; no legacy fallback."""
    if scope not in {"single", "pair"}:
        raise ValueError("noise scope must be single or pair (device flicker only)")
    _source, groups = _grouped_samples(result)
    rows = _select_group(groups, operating_point_id)
    if not rows:
        return []
    predicted = "dynamic_dBc_Hz" if scope == "single" else "predicted_pair_dBc_Hz"
    label = "single device flicker" if scope == "single" else "independent device pair flicker"
    if noise_kind not in {"flicker", "thermal", "both"}:
        raise ValueError("invalid noise kind")
    if noise_kind != "flicker":
        series = []
        if noise_kind == "both":
            series.append(_series("flicker", "ISF flicker · symmetric gm pair", rows, [row[predicted] for row in rows], scope=scope))
        series.append(_series("thermal", "ISF thermal · gm pair + LC", rows, [row["thermal_dBc_Hz"] for row in rows], scope="pair"))
        if noise_kind == "both":
            series.append(_series("calculated", "Calculated total · flicker + thermal", rows, [row["predicted_total_dBc_Hz"] for row in rows], scope="pair"))
        series.append(_series("spectre", "VCO simulated total phase noise · PM-SSB (all noise sources)", rows, [row.get("spectre_total_dBc_Hz") for row in rows], scope="whole_vco"))
        return series
    return [_series("calculated", "ISF calculated · " + label, rows, [row[predicted] for row in rows], scope=scope),
            _series("spectre", "VCO simulated total phase noise · PM-SSB (all noise sources)", rows,
                    [row.get("spectre_total_dBc_Hz") for row in rows], scope="whole_vco")]


def noise_contribution_rows(summary, scope=None, noise_kind="both"):
    """One result-owned sample: overlapping aggregates, then raw sources."""
    row = enrich_summary(summary)
    carrier = _value(row, "carrier_peak_V")
    total_asd = _value(row, "spectre_total_pm_asd_V_rtHz")
    total_psd = total_asd ** 2 if total_asd is not None and total_asd > 0 else None
    output = []

    def add(label, device, mechanism, asd, simulated, calculated=None, kind="aggregate", psd=None, calculated_psd=None):
        if psd is None and asd is not None and asd >= 0:
            psd = asd ** 2
        output.append({"label": label, "device": device, "mechanism": mechanism,
                       "simulated_dbc": simulated, "calculated_dbc": calculated,
                       "fraction_pct": 100.0 * psd / total_psd if psd is not None and total_psd else None,
                       "psd_V2_Hz": psd, "calculated_psd_V2_Hz": calculated_psd,
                       "asd_V_rtHz": asd, "kind": kind})

    add("VCO simulated total phase noise", "all devices", "all mechanisms · PM-SSB", total_asd,
        row.get("spectre_total_dBc_Hz"))
    generated = row.get("replica_source") == "generated"
    if (scope != "pair" or generated) and noise_kind != "thermal":
        add("target single device flicker comparison", row.get("target_noise_trace") or "target device", "fn", _value(row, "spectre_fn_pm_asd_V_rtHz"),
            row.get("spectre_fn_dBc_Hz"), row.get("dynamic_dBc_Hz"))
    if noise_kind != "thermal":
        pair_label = "symmetric pair estimate · +3.01 dB" if generated else "symmetric gm pair · flicker comparison"
        add(pair_label, "Target + Peer device", "fn", _value(row, "spectre_pair_pm_asd_V_rtHz"),
            row.get("spectre_pair_dBc_Hz"), row.get("predicted_pair_dBc_Hz"))
        output[-1]["comparison_group"] = "flicker_pair"
        identity = row.get("flicker_pair_identity") or {}
        output[-1]["members"] = ([identity["target_noise_trace"], identity["peer_noise_trace"]]
                                  if identity and not generated else [])
    thermal = row.get("thermal_noise") or {}
    predicted_sources = {(item["device"], item["mechanism"]): item
                         for item in thermal.get("sources", [])} if thermal.get("status") == "available" else {}
    contributions = row.get("spectre_noise_contributions")
    if not isinstance(contributions, list):
        contributions = []
    references_by_source = {}
    for item in contributions:
        if not isinstance(item, Mapping):
            continue
        identity = item.get("device"), item.get("mechanism")
        if all(isinstance(value, str) and value.strip() for value in identity):
            references_by_source.setdefault(identity, []).append(item)

    def unique_reference_psd(identity):
        matches = references_by_source.get(identity, [])
        if len(matches) != 1:
            return None
        power = _value(matches[0], "psd_V2_Hz")
        return power if power is not None and power >= 0 else None

    if noise_kind != "flicker" and predicted_sources:
        reference_issues = []
        for identity in sorted(predicted_sources):
            matches = references_by_source.get(identity, [])
            if unique_reference_psd(identity) is None:
                reason = "missing" if not matches else "duplicate" if len(matches) > 1 else "invalid_psd"
                reference_issues.append({"device": identity[0], "mechanism": identity[1], "reason": reason})
        # A partial reference cannot be compared against the full prediction.
        # Zero is a measured PSD; missing, invalid and duplicate are not zero.
        reference_psd = (math.fsum(unique_reference_psd(identity) for identity in predicted_sources)
                         if not reference_issues else None)
        reference_asd = math.sqrt(reference_psd) if reference_psd is not None else None
        label = "modeled thermal comparison" + (" (reference sources incomplete)" if reference_issues else "")
        add(label, "gm pair + LC", "thermal", reference_asd,
            dBc_from_pm_asd(reference_asd, carrier) if reference_asd is not None and carrier else None,
            row["thermal_dBc_Hz"])
        output[-1].update(reference_complete=not reference_issues, reference_issues=reference_issues)

    target = row.get("target_noise_trace") if generated else thermal.get("target_noise_trace")
    peer = thermal.get("peer_noise_trace")
    target_owned = ((generated or thermal.get("status") == "available") and isinstance(target, str) and bool(target.strip()))
    pair_dbc = _value(row, "predicted_pair_dBc_Hz")
    peer_owned = (not generated and target_owned and isinstance(peer, str) and bool(peer.strip()) and peer != target
                  and thermal.get("pair_model") == "symmetric_independent"
                  and row.get("pair_model") == "symmetric_independent"
                  and pair_dbc is not None)
    if isinstance(contributions, list):
        details = []
        for item in contributions:
            if not isinstance(item, Mapping):
                continue
            psd = _value(item, "psd_V2_Hz")
            if psd is None or psd < 0:
                continue
            details.append((psd, item))
        for psd, item in sorted(details, key=lambda entry: (-entry[0], str(entry[1].get("device")), str(entry[1].get("mechanism")))):
            asd = math.sqrt(psd)
            device, mechanism = str(item.get("device") or "unknown device"), str(item.get("mechanism") or "unknown mechanism")
            is_thermal = (mechanism == "rn" or (device, mechanism) in predicted_sources
                          or item.get("noise_family") in {"channel_thermal", "resistive_thermal"})
            if (noise_kind == "flicker" and mechanism != "fn") or (noise_kind == "thermal" and not is_thermal):
                continue
            calculated = None
            calculated_psd = None
            if unique_reference_psd((device, mechanism)) is not None:
                predicted = predicted_sources.get((device, mechanism), {})
                calculated = predicted.get("phase_noise_dBc_Hz")
                calculated_psd = predicted.get("psd_V2_Hz")
                if mechanism == "fn" and ((target_owned and device == target) or (peer_owned and device == peer)):
                    calculated = row.get("dynamic_dBc_Hz")
                    amplitude = _value(row, "dynamic_pm_asd_V_rtHz")
                    calculated_psd = amplitude ** 2 if amplitude is not None and amplitude >= 0 else None
            add(device + " · " + mechanism, device, mechanism, asd,
                dBc_from_pm_asd(asd, carrier) if carrier is not None else None,
                calculated=calculated, kind="source", psd=psd, calculated_psd=calculated_psd)
            identity = row.get("flicker_pair_identity") or {}
            if (not generated and mechanism == "fn"
                    and device in (identity.get("target_noise_trace"), identity.get("peer_noise_trace"))):
                output[-1]["comparison_group"] = "flicker_pair"
    if noise_kind != "flicker":
        for identity, source in sorted(predicted_sources.items()):
            matches = references_by_source.get(identity, [])
            if not matches or (len(matches) == 1 and unique_reference_psd(identity) is None):
                device, mechanism = identity
                label = " (invalid simulation reference)" if matches else " (simulation reference missing)"
                add(device + " · " + mechanism + label, device, mechanism,
                    None, None, calculated=source.get("phase_noise_dBc_Hz"), kind="source",
                    calculated_psd=source.get("psd_V2_Hz"))
    return output



def phase_noise_comparison_series(result, operating_point_id=None, noise_kind="flicker"):
    """Match independent Spectre and ISF source scopes before subtracting dB."""
    source, groups = _grouped_samples(result)
    rows = _select_group(groups, operating_point_id)
    single = is_generated_flicker(source)
    if noise_kind not in {"flicker", "thermal", "both"}:
        raise ValueError("invalid noise kind")
    if not rows:
        return {"prediction": None, "reference": None, "error": None}
    if noise_kind == "flicker":
        scope = "selected gm · fn" if single else "gm pair · fn"
    elif noise_kind == "thermal":
        scope = "modeled thermal"
    else:
        scope = "gm pair · fn + modeled thermal"

    predicted, measured = [], []
    for row in rows:
        flicker_reference = row["spectre_fn_dBc_Hz" if single and noise_kind == "flicker" else "spectre_pair_dBc_Hz"]
        if noise_kind == "flicker":
            predicted.append(row["dynamic_dBc_Hz" if single else "predicted_pair_dBc_Hz"])
            measured.append(flicker_reference)
            continue
        thermal_reference = next(
            (item["simulated_dbc"] for item in noise_contribution_rows(row, "pair", "thermal")
             if item.get("reference_complete") is True),
            None,
        )
        if noise_kind == "thermal":
            predicted.append(row["thermal_dBc_Hz"])
            measured.append(thermal_reference)
            continue
        predicted.append(row["predicted_total_dBc_Hz"])
        if thermal_reference is None or flicker_reference is None:
            measured.append(None)
        else:
            high, low = sorted((flicker_reference, thermal_reference), reverse=True)
            measured.append(high + 10 * math.log10(1 + 10 ** ((low - high) / 10)))

    difference = [prediction - reference if prediction is not None and reference is not None else None
                  for prediction, reference in zip(predicted, measured)]
    prediction = _series("matched_prediction", "ISF single device · fn", rows, predicted, scope=scope) if single and noise_kind == "flicker" else None
    reference = _series("matched_reference", "HBnoise · " + scope, rows, measured, scope=scope)
    error = _series("matched_error", "ISF − HBnoise · " + scope, rows, difference, scope=scope)
    error["unit"] = "dB"
    return {"prediction": prediction, "reference": reference, "error": error}

def _sample_harmonics(source, rows):
    sample_harmonics = []
    for summary in rows:
        harmonics = {}
        for row in source.get("points", {}).get(str(summary.get("point")), {}).get("harmonics", []):
            k = _number(row.get("k"))
            if k is None or k < 0 or not k.is_integer():
                raise ValueError("invalid harmonic order in result")
            if int(k) in harmonics:
                raise ValueError("duplicate harmonic order in result")
            harmonics[int(k)] = row
        sample_harmonics.append(harmonics)
    return sample_harmonics


def harmonic_noise_series(result, operating_point_id=None, scope="single", noise_kind="flicker"):
    """Legacy flicker equivalents for exports; other modes use input-noise bands."""
    source, groups = _grouped_samples(result)
    rows = _select_group(groups, operating_point_id)
    if not rows:
        return []
    sample_harmonics = _sample_harmonics(source, rows)
    orders = sorted({k for sample in sample_harmonics for k in sample})
    prefix = "pair_" if scope == "pair" else ""
    series = []
    if noise_kind == "flicker":
        series = [_series("harmonic_" + str(k), "flicker k={} single-order equivalent".format(k), rows,
                          [sample.get(k, {}).get(prefix + "harmonic_equivalent_dBc_Hz") for sample in sample_harmonics], scope=scope, harmonic=k)
                  for k in orders]
        series.append(_series("coherent_total", "flicker coherent sum of all harmonics · " + ("symmetric gm pair" if scope == "pair" else "single device"), rows,
                              [row["predicted_pair_dBc_Hz" if scope == "pair" else "dynamic_dBc_Hz"] for row in rows], scope=scope))
    if noise_kind != "flicker":
        # k pairs belong to c0_eff. They must never be matched to input band m.
        return input_band_noise_series(result, operating_point_id, noise_kind)["series"]

    return series


def cumulative_noise_series(result, operating_point_id=None, k=0, scope="single", noise_kind="flicker"):
    """One curve of the stored coherent sum through k; missing order is a gap."""
    if noise_kind == "thermal":
        return []
    order = _number(k)
    if order is None or order < 0 or not order.is_integer():
        raise ValueError("cumulative harmonic order must be a non-negative integer")
    order = int(order)
    source, groups = _grouped_samples(result)
    rows = _select_group(groups, operating_point_id)
    if not rows:
        return []
    samples = _sample_harmonics(source, rows)
    return [_series("cumulative_" + str(order), "Cumulative through k={} · coherent sum".format(order), rows,
                    [sample.get(order, {}).get("pair_cumulative_dBc_Hz" if scope == "pair" else "cumulative_dBc_Hz") for sample in samples], scope=scope, harmonic=order)]


__all__ = ["SUMMARY_QUALITY_FIELDS", "quality_fields", "enrich_summary", "enrich_harmonic", "enrich_result",
           "operating_points", "phase_noise_series", "noise_contribution_rows", "harmonic_noise_series", "cumulative_noise_series"]


# Fixed categorical colors are shared by all presentation surfaces/themes.
BAND_COLORS = ("#33D1DE", "#F2C230", "#EC5AA5", "#93D65E", "#9C8CFF",
               "#F28C4B", "#7FB5FF", "#E6E6E6", "#7c9eeb", "#ad83a4")


def band_color(order):
    if order < len(BAND_COLORS):
        return BAND_COLORS[order]
    import colorsys
    rgb = colorsys.hsv_to_rgb((order * .61803398875) % 1, .58, .82)
    return "#{:02x}{:02x}{:02x}".format(*(round(v * 255) for v in rgb))


def fit_noise_slope(points):
    """OLS dB/dec over valid samples in the lowest two decadess (no interpolation)."""
    pairs = [(math.log10(x), y) for x, y in points
             if _number(x) is not None and x > 0 and _number(y) is not None]
    if len({x for x, _ in pairs}) < 2:
        return None
    low = min(x for x, _ in pairs)
    pairs = [(x, y) for x, y in pairs if x <= low + 2 + 1e-12]
    mx = math.fsum(x for x, _ in pairs) / len(pairs)
    my = math.fsum(y for _, y in pairs) / len(pairs)
    denominator = math.fsum((x - mx) ** 2 for x, _ in pairs)
    return math.fsum((x - mx) * (y - my) for x, y in pairs) / denominator if denominator else None


def input_band_noise_series(result, operating_point_id=None, noise_kind="both"):
    """Fig.12 stationary-input bands; independent powers, not Fourier Qk pairs.

    Uses result-owned pair flicker ASD (the coherent sum, sqrt(2)*|sum Qk|).
    Missing mechanisms stay explicitly partial, never change calculated totals.
    """
    if noise_kind not in {"both", "flicker", "thermal"}:
        raise ValueError("invalid noise kind")
    _, groups = _grouped_samples(result)
    rows = _select_group(groups, operating_point_id)
    powers, missing = [], set()
    for row in rows:
        bands = {}
        budget = row.get("thermal_noise") or {}
        if noise_kind != "flicker":
            if budget.get("status") == "available":
                for source in budget["sources"]:
                    for part in source["input_band_contributions"]:
                        n = abs(part["input_band"])
                        bands[n] = bands.get(n, 0.) + part["psd_V2_Hz"]
            else:
                missing.add("This result has no valid thermal data" + (": " + str(budget["reason"]) if budget.get("reason") else ""))
        if noise_kind != "thermal":
            asd = _value(row, "predicted_pair_pm_asd_V_rtHz")
            if asd is not None:
                bands[0] = bands.get(0, 0.) + asd ** 2
            else:
                missing.add("This result has no two-device flicker coherent-sum data")
        powers.append(bands)
    orders = sorted({n for bands in powers for n in bands})
    output = []
    for n in orders:
        values = [dBc_from_pm_asd(math.sqrt(bands[n]), row["carrier_peak_V"])
                  if n in bands and row.get("carrier_peak_V") else None for bands, row in zip(powers, rows)]
        label = "n=0 · near DC" if n == 0 else "n={} · near {}f0".format(n, "" if n == 1 else str(n)+"·")
        trace = _series("input_band_"+str(n), label, rows, values, scope="pair", harmonic=n)
        trace.update(n=n, color=band_color(n), row_key="band:"+str(n),
                     power_V2_Hz=[bands.get(n) for bands in powers],
                     fraction=[bands[n]/math.fsum(bands.values()) if n in bands and math.fsum(bands.values()) else None for bands in powers])
        output.append(trace)
    totals = [dBc_from_pm_asd(math.sqrt(math.fsum(bands.values())), row["carrier_peak_V"])
              if bands and row.get("carrier_peak_V") else None for bands, row in zip(powers, rows)]
    total = _series("calculated_total", "sum of included bands" if missing else "sum of all band powers", rows, totals, scope="pair")
    total.update(width=3.5, role="total", row_key="band:total")
    if orders:
        output.append(total)
    reference = _series("hbnoise_total", "HBnoise simulated total PN", rows,
                        [r.get("spectre_total_dBc_Hz") for r in rows], scope="whole_vco")
    reference.update(dash=True, color="#9b8790", width=2, row_key="band:reference")
    output.append(reference)
    for trace in output:
        trace["slope"] = fit_noise_slope(trace["points"])
        trace["keys"] = [trace["row_key"]] * len(rows)
        slope = trace["slope"]
        trace["name"] += " · {:.1f} dB/dec".format(slope) if slope is not None else " · slope unavailable"
    notes = sorted(missing)
    if len(rows) == 1:
        notes.append("Only one offset; slope unavailable")
    return {"series": output, "rows": rows, "powers": powers, "notes": notes,
            "single_offset": len(rows) == 1, "partial": bool(missing)}


def harmonic_projection_fractions(harmonics):
    """Signed coherent projection; undefined total remains None, never zero-filled."""
    values = [complex(r["Q_real_V_rtHz"], r["Q_imag_V_rtHz"])
              if _number(r.get("Q_real_V_rtHz")) is not None and _number(r.get("Q_imag_V_rtHz")) is not None else None for r in harmonics]
    total = sum(values, 0j) if values and all(q is not None for q in values) else None
    denominator = abs(total)**2 if total is not None else 0
    return [dict(r, projection_fraction=(q*total.conjugate()).real/denominator if q is not None and denominator else None,
                 cancellation=bool(q is not None and total is not None and abs(q)>abs(total)))
            for r, q in zip(harmonics, values)]
