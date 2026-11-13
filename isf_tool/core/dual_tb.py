"""Dedicated importer and recompute path for canonical two-testbench CSVs."""

from __future__ import unicode_literals

import cmath
import csv
import io
import itertools
import json
import math
import os

from .. import calculation as shared_calculation

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

from .bundle import _coerce_value
from .errors import BundleImportError, ValidationError
from .limits import MAX_PHASE_CANDIDATE_HARMONICS
from .models import empty_point_result, empty_result, normalize_result
from .result_presentation import enrich_result, quality_fields


_VCO_REQUIRED = frozenset(
    (
        "point",
        "k",
        "corner",
        "f0_Hz",
        "carrier_real_V",
        "carrier_imag_V",
        "H_real_ohm",
        "H_imag_ohm",
        "spectre_pm_asd_V_rtHz",
    )
)
_REPLICA_REQUIRED = frozenset(
    (
        "point",
        "k",
        "Id_real_A",
        "Id_imag_A",
        "Mdc_real_A_rtHz",
        "Mdc_imag_A_rtHz",
    )
)
_SIGNOFF_PAIR_FIELDS = (
    "second_device_predicted_pm_asd_V_rtHz",
    "spectre_pair_pm_asd_V_rtHz",
)


def _path(value, label):
    try:
        value = os.fspath(value)
    except (AttributeError, TypeError):
        pass
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str) or not value:
        raise ValidationError("%s must be a path string" % label)
    return os.path.realpath(os.path.abspath(os.path.expanduser(value)))


def _read_csv(path, required, encoding):
    from .limits import MAX_CANONICAL_CSV_FIELD_BYTES
    csv.field_size_limit(MAX_CANONICAL_CSV_FIELD_BYTES)
    if not os.path.isfile(path):
        raise BundleImportError("canonical CSV does not exist: %s" % path)
    with io.open(path, "r", encoding=encoding, newline="") as stream:
        reader = csv.DictReader(stream)
        headers = [(header or "").strip() for header in (reader.fieldnames or [])]
        if not headers or len(headers) != len(set(headers)):
            raise BundleImportError("canonical CSV has invalid headers: %s" % path)
        missing = sorted(required - set(headers))
        if missing:
            raise BundleImportError(
                "canonical CSV is missing required columns: %s" % ", ".join(missing),
                details={"path": path, "missing": missing},
            )
        reader.fieldnames = headers
        rows = []
        for raw in reader:
            extras = raw.pop(None, None)
            if extras and any(str(item).strip() for item in extras if item is not None):
                raise BundleImportError(
                    "canonical CSV row has extra fields: %s line %d"
                    % (path, reader.line_num)
                )
            if not any(value not in (None, "") for value in raw.values()):
                continue
            rows.append(dict((key, _coerce_value(raw.get(key))) for key in headers))
    return rows


def _read_metadata(path, encoding):
    if path is None or not os.path.isfile(path):
        return {}
    try:
        with io.open(path, "r", encoding=encoding) as stream:
            value = json.load(stream)
    except (ValueError, UnicodeError, IOError) as exc:
        raise BundleImportError("cannot read dual-TB metadata: %s" % exc)
    if not isinstance(value, Mapping):
        raise BundleImportError("dual-TB metadata must be a JSON object")
    return dict(value)


def _resolve_files(options):
    base = options.get("path", options.get("bundle_root"))
    if base is not None:
        base = _path(base, "dual-TB bundle path")
        if not os.path.isdir(base):
            raise BundleImportError("dual-TB bundle path must be a directory: %s" % base)
        canonical = os.path.join(base, "canonical")
        if (
            not os.path.isfile(os.path.join(base, "vco_results.csv"))
            and os.path.isdir(canonical)
        ):
            base = canonical

    def selected_path(*names):
        for name in names:
            value = options.get(name)
            if value not in (None, ""):
                return _path(value, name)
        return None

    vco_path = selected_path("vco_path", "vco_csv")
    replica_path = selected_path("replica_path", "replica_csv")
    workpoint_path = selected_path("workpoint_path", "workpoint_csv")
    metadata_path = selected_path("metadata_path")
    if base is not None:
        vco_path = vco_path or os.path.join(base, "vco_results.csv")
        replica_path = replica_path or os.path.join(base, "replica_results.csv")
        candidate_workpoint = os.path.join(base, "workpoint.csv")
        candidate_metadata = os.path.join(base, "metadata.json")
        workpoint_path = workpoint_path or (
            candidate_workpoint if os.path.isfile(candidate_workpoint) else None
        )
        metadata_path = metadata_path or (
            candidate_metadata if os.path.isfile(candidate_metadata) else None
        )
    if vco_path is None or replica_path is None:
        raise ValidationError(
            "dual-TB import requires vco_results.csv and replica_results.csv"
        )
    return vco_path, replica_path, workpoint_path, metadata_path


def _finite_number(row, key, label):
    value = row.get(key)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BundleImportError("%s.%s must be numeric" % (label, key))
    if not math.isfinite(number):
        raise BundleImportError("%s.%s must be finite" % (label, key))
    return number


def spectre_noise_evidence(row, label="VCO noise reference"):
    """Decode optional total/contributor evidence without reinterpreting legacy data.

    Earlier raw summaries used ``spectre_total_pm_asd_V_rtHz`` for a single
    MOS's total noise. Only an explicit whole-VCO provenance marker grants
    the new meaning; a missing marker must stay unavailable, not a fallback.
    """
    def decode(key, expected_type, default):
        value = row.get(key)
        if value in (None, ""):
            value = row.get(key + "_json")
        if value in (None, ""):
            return default
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise BundleImportError("%s.%s is invalid JSON" % (label, key)) from exc
        if not isinstance(value, expected_type):
            raise BundleImportError("%s.%s has an invalid type" % (label, key))
        return value

    reference = decode("spectre_noise_reference", Mapping, {})
    output = {"spectre_total_pm_asd_V_rtHz": None,
              "spectre_noise_contributions": [], "spectre_noise_reference": {}}
    if isinstance(reference.get("schema_version"), bool) or reference.get("schema_version") != 1 or reference.get("scope") != "whole_vco":
        return output
    asd = None
    if row.get("spectre_total_pm_asd_V_rtHz") not in (None, ""):
        asd = _finite_number(row, "spectre_total_pm_asd_V_rtHz", label)
        if asd < 0:
            raise BundleImportError("%s total noise ASD must be non-negative" % label)
    if reference.get("offset_Hz") not in (None, ""):
        reference_offset = _finite_number(reference, "offset_Hz", label)
        row_offset = row.get("offset_Hz", row.get("offset_hz"))
        if row_offset not in (None, "") and reference_offset != _finite_number({"offset_Hz": row_offset}, "offset_Hz", label):
            raise BundleImportError("%s total noise reference offset mismatch" % label)
    if reference.get("total_out_psd_V2_Hz") not in (None, ""):
        reference_psd = _finite_number(reference, "total_out_psd_V2_Hz", label)
        if reference_psd < 0 or (asd is not None and not math.isclose(asd * asd, reference_psd, rel_tol=1e-10, abs_tol=1e-40)):
            raise BundleImportError("%s total noise reference ASD/PSD mismatch" % label)
    contributions = decode("spectre_noise_contributions", list, [])
    normalized, seen = [], set()
    for index, item in enumerate(contributions):
        if not isinstance(item, Mapping):
            raise BundleImportError("%s contributor %d must be an object" % (label, index))
        device, mechanism = item.get("device"), item.get("mechanism")
        if not isinstance(device, str) or not device or not isinstance(mechanism, str) or not mechanism:
            raise BundleImportError("%s contributor must name its device and mechanism" % label)
        identity = (device, mechanism)
        if identity in seen:
            raise BundleImportError("%s contains a duplicate noise contributor" % label)
        seen.add(identity)
        psd = _finite_number(item, "psd_V2_Hz", label)
        if psd < 0:
            raise BundleImportError("%s contributor PSD must be non-negative" % label)
        if mechanism == "total":
            raise BundleImportError("%s mechanism table must not mix device totals and components" % label)
        if item.get("asd_V_rtHz") not in (None, ""):
            item_asd = _finite_number(item, "asd_V_rtHz", label)
            if item_asd < 0 or not math.isclose(item_asd * item_asd, psd, rel_tol=1e-10, abs_tol=1e-40):
                raise BundleImportError("%s contributor ASD/PSD mismatch" % label)
        normalized.append(dict(item, device=device, mechanism=mechanism,
                               psd_V2_Hz=psd, asd_V_rtHz=math.sqrt(psd)))
    output.update(spectre_total_pm_asd_V_rtHz=asd,
                  spectre_noise_contributions=normalized,
                  spectre_noise_reference=dict(reference))
    return output


def _integer_harmonic(row, label):
    value = _finite_number(row, "k", label)
    integer = int(value)
    if value != integer:
        raise BundleImportError("%s.k must be an integer" % label)
    return integer


def _group_by_point(rows, label):
    grouped = {}
    for index, row in enumerate(rows):
        point = row.get("point")
        if point in (None, ""):
            raise BundleImportError("%s row %d has no point" % (label, index + 2))
        grouped.setdefault(str(point), []).append(row)
    return grouped


def _validate_point_sample_identity(point_name, vrows, rrows, wrows, options, metadata):
    """Reject mixed scalar samples even when their harmonic orders do not overlap.

    Optional identity columns remain optional for legacy bundles.  Every value
    that is provided must agree with the VCO point's declared scalar identity;
    replica/workpoint rows may not silently introduce a different offset.
    """
    first = vrows[0]
    offset = first.get("offset_Hz", first.get("offset_hz", options.get("offset_hz", metadata.get("offset_hz"))))
    expected = {"offset_Hz": offset, "f0_Hz": first.get("f0_Hz")}
    for field, value in list(expected.items()):
        if value not in (None, ""):
            expected[field] = _finite_number({field: value}, field, point_name)
    text_identity = {
        "corner": first.get("corner"),
        "operating_point_id": first.get("operating_point_id") or point_name,
    }
    for label, rows in (("vco_results", vrows), ("replica_results", rrows), ("workpoint", wrows)):
        for index, row in enumerate(rows):
            location = "%s.%s[%d]" % (point_name, label, index)
            for field, aliases in (("offset_Hz", ("offset_Hz", "offset_hz")), ("f0_Hz", ("f0_Hz",))):
                baseline = expected[field]
                if baseline in (None, ""):
                    continue  # The normal scalar metadata gate reports absence.
                for alias in aliases:
                    if row.get(alias) in (None, ""):
                        continue
                    actual = _finite_number(row, alias, location)
                    # Different requested offsets must never be merged, even
                    # when very close.  f0 allows serialization roundoff only.
                    equal = actual == baseline if field == "offset_Hz" else math.isclose(actual, baseline, rel_tol=1e-12, abs_tol=0.0)
                    if not equal:
                        raise BundleImportError(
                            "inconsistent %s within scalar point %s; separate operating points and offsets" % (field, point_name),
                            details={"point": point_name, "field": alias, "row": location, "expected": baseline, "actual": actual},
                        )
            for field, baseline in text_identity.items():
                if row.get(field) in (None, ""):
                    continue
                if str(row[field]) != str(baseline):
                    raise BundleImportError(
                        "inconsistent %s within scalar point %s" % (field, point_name),
                        details={"point": point_name, "field": field, "row": location, "expected": baseline, "actual": row[field]},
                    )


def _validate_signoff_pair_fields(point_name, rows):
    values = {}
    for key in _SIGNOFF_PAIR_FIELDS:
        point_values = []
        for index, row in enumerate(rows):
            if row.get(key) in (None, ""):
                raise BundleImportError(
                    "signoff requires %s on every VCO row for %s"
                    % (key, point_name)
                )
            value = _finite_number(
                row, key, "%s.vco[%d]" % (point_name, index)
            )
            if value <= 0.0:
                raise BundleImportError(
                    "signoff requires positive %s for %s" % (key, point_name)
                )
            point_values.append(value)
        first = point_values[0]
        if any(
            not math.isclose(value, first, rel_tol=1.0e-12, abs_tol=1.0e-30)
            for value in point_values[1:]
        ):
            raise BundleImportError(
                "signoff requires one consistent %s value for %s"
                % (key, point_name)
            )
        values[key] = first
    return values


def _wrap_phase(value):
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def _candidate_sets(id_harmonics, am_asd, pm_asd, dc_coeffs, previous=None):
    harmonics = sorted(set(am_asd) & set(pm_asd))
    if len(harmonics) > MAX_PHASE_CANDIDATE_HARMONICS:
        raise BundleImportError(
            "phase-candidate search has %d harmonics; the exhaustive 2^N "
            "backend limit is %d"
            % (len(harmonics), MAX_PHASE_CANDIDATE_HARMONICS),
            details={
                "field": "max_harmonic",
                "harmonic_count": len(harmonics),
                "maximum": MAX_PHASE_CANDIDATE_HARMONICS,
                "candidate_count_exponent": len(harmonics),
            },
        )
    candidates = []
    for signs in itertools.product((-1, 1), repeat=len(harmonics)):
        selected = {}
        phase_cost = 0.0
        delays = []
        continuity_cost = 0.0
        for harmonic, sign in zip(harmonics, signs):
            theta = cmath.phase(id_harmonics[harmonic])
            value = cmath.exp(1j * theta) * (
                am_asd[harmonic] + 1j * sign * pm_asd[harmonic]
            )
            selected[harmonic] = value
            if dc_coeffs.get(harmonic):
                delta = _wrap_phase(cmath.phase(value) - cmath.phase(dc_coeffs[harmonic]))
                phase_cost += (delta / math.pi) ** 2
                delays.append(delta / float(harmonic))
            if previous and harmonic in previous and previous[harmonic]:
                continuity_delta = _wrap_phase(
                    cmath.phase(value) - cmath.phase(previous[harmonic])
                )
                continuity_cost += (continuity_delta / math.pi) ** 2
        if len(delays) > 1:
            mean = sum(delays) / float(len(delays))
            delay_cost = sum((item - mean) ** 2 for item in delays) / float(len(delays))
        else:
            delay_cost = 0.0
        candidates.append(
            {
                "signs": "".join("+" if sign > 0 else "-" for sign in signs),
                "selected": selected,
                "phase_cost": phase_cost,
                "delay_cost": delay_cost,
                "continuity_cost": continuity_cost,
                "score": phase_cost + 0.5 * delay_cost + 0.35 * continuity_cost,
            }
        )
    candidates.sort(key=lambda item: item["score"])
    return candidates


def _signed_real(positive):
    signed = {0: complex(positive[0])}
    for harmonic, value in positive.items():
        if harmonic > 0:
            signed[harmonic] = complex(value)
            signed[-harmonic] = complex(value).conjugate()
    return signed


def _coherent_sum(transfer, source):
    return sum(
        (complex(transfer[key]) * complex(source[key]) for key in set(transfer) & set(source)),
        0j,
    )


def _fit_delay(mdc, mdyn):
    terms = []
    previous = None
    shift = 0.0
    for harmonic in sorted(set(mdc) & set(mdyn)):
        if harmonic <= 0 or not mdc[harmonic] or not mdyn[harmonic]:
            continue
        phase = _wrap_phase(cmath.phase(mdyn[harmonic]) - cmath.phase(mdc[harmonic]))
        if previous is not None:
            while phase + shift - previous > math.pi:
                shift -= 2.0 * math.pi
            while phase + shift - previous < -math.pi:
                shift += 2.0 * math.pi
        phase += shift
        previous = phase
        terms.append((float(harmonic), phase, abs(mdc[harmonic]) ** 2))
    denominator = sum(weight * harmonic * harmonic for harmonic, _phase, weight in terms)
    if denominator == 0.0:
        return 0.0
    return -sum(weight * harmonic * phase for harmonic, phase, weight in terms) / denominator


def _error_db(predicted, reference):
    if predicted <= 0.0 or reference <= 0.0:
        return None
    return 20.0 * math.log10(predicted / reference)


def _dbc(asd, carrier):
    if asd <= 0.0 or carrier <= 0.0:
        return None
    return 10.0 * math.log10(2.0 * asd * asd / (carrier * carrier))


def _phase_deg(value):
    return math.degrees(cmath.phase(value)) if value else 0.0


def _close_complex(left, right, rel_tol=1.0e-8, abs_tol=1.0e-30):
    return abs(left - right) <= max(abs_tol, rel_tol * max(abs(left), abs(right)))


_SIGNOFF_WORKPOINT_COLUMNS = frozenset(
    (
        "time_s", "wt_rad", "VG_V", "VD_V", "VS_V", "VB_V",
        "ID_main_A", "ID_replica_A", "m_DC_A_rtHz", "m_dyn_A_rtHz",
        "isf_i", "isf_q", "product_i", "product_q",
    )
)


def _validate_signoff_workpoint(
    point_name,
    rows,
    f0_hz,
    mdc,
    mdyn,
    id_harmonics,
    transfer,
    carrier_phasor,
    offset_hz,
):
    if len(rows) < 3:
        raise BundleImportError("signoff requires a complete workpoint period for %s" % point_name)
    missing = sorted(_SIGNOFF_WORKPOINT_COLUMNS - set(rows[0]))
    if missing:
        raise BundleImportError(
            "signoff workpoint is missing columns for %s: %s"
            % (point_name, ", ".join(missing))
        )
    numeric = []
    for index, row in enumerate(rows):
        values = {}
        for key in _SIGNOFF_WORKPOINT_COLUMNS:
            values[key] = _finite_number(row, key, "%s.workpoint[%d]" % (point_name, index))
        numeric.append(values)
    phases = [row["wt_rad"] for row in numeric]
    if any(right <= left for left, right in zip(phases, phases[1:])):
        raise BundleImportError("workpoint phase grid is not strictly increasing for %s" % point_name)
    if abs(phases[0]) > 1.0e-9 or abs(phases[-1] - 2.0 * math.pi) > 1.0e-7:
        raise BundleImportError("workpoint must span one unshifted 0..2pi period for %s" % point_name)
    phase_steps = [right - left for left, right in zip(phases, phases[1:])]
    mean_step = sum(phase_steps) / len(phase_steps)
    if max(abs(step - mean_step) for step in phase_steps) > max(1.0e-12, abs(mean_step) * 1.0e-8):
        raise BundleImportError("workpoint phase grid is not uniform for %s" % point_name)
    period = 1.0 / f0_hz
    times = [row["time_s"] for row in numeric]
    if any(right <= left for left, right in zip(times, times[1:])):
        raise BundleImportError("workpoint time grid is not strictly increasing for %s" % point_name)
    if not math.isclose(times[-1] - times[0], period, rel_tol=1.0e-7, abs_tol=1.0e-18):
        raise BundleImportError("workpoint time span is not one oscillator period for %s" % point_name)
    inferred_origins = [
        time_s - phase / (2.0 * math.pi * f0_hz)
        for time_s, phase in zip(times, phases)
    ]
    t0_spread = max(inferred_origins) - min(inferred_origins)
    if t0_spread > max(1.0e-18, period * 1.0e-8):
        raise BundleImportError("workpoint columns do not share one time origin for %s" % point_name)

    endpoint_fields = _SIGNOFF_WORKPOINT_COLUMNS - frozenset(("time_s", "wt_rad"))
    for key in endpoint_fields:
        left = numeric[0][key]
        right = numeric[-1][key]
        if not math.isclose(left, right, rel_tol=1.0e-6, abs_tol=1.0e-18):
            raise BundleImportError("workpoint %s is not periodic for %s" % (key, point_name))
    for branch in ("i", "q"):
        product_error = max(
            abs(
                row["product_%s" % branch]
                - row["isf_%s" % branch] * row["m_dyn_A_rtHz"]
            )
            for row in numeric
        )
        product_scale = max(
            1.0e-30,
            max(abs(row["product_%s" % branch]) for row in numeric),
        )
        if product_error > product_scale * 1.0e-8:
            raise BundleImportError(
                "workpoint ISF_%s*mDyn product is inconsistent for %s"
                % (branch.upper(), point_name)
            )

    samples = numeric[:-1]
    count = float(len(samples))

    def coefficient(key, harmonic):
        return sum(
            row[key] * cmath.exp(-1j * harmonic * row["wt_rad"])
            for row in samples
        ) / count

    gamma_branches = shared_calculation.carrier_referenced_gamma_branches(
        transfer,
        carrier_phasor,
        offset_hz,
        max(mdyn),
    )
    for branch in ("i", "q"):
        for harmonic, expected in gamma_branches[branch].items():
            actual = coefficient("isf_%s" % branch, harmonic)
            if not _close_complex(
                actual, expected, rel_tol=2.0e-6, abs_tol=1.0e-18
            ):
                raise BundleImportError(
                    "workpoint ISF_%s[%d] is not derivable from signed H/carrier for %s"
                    % (branch.upper(), harmonic, point_name)
                )

    for harmonic in sorted(mdc):
        for key, expected in (
            ("m_DC_A_rtHz", mdc[harmonic]),
            ("m_dyn_A_rtHz", mdyn[harmonic]),
            ("ID_replica_A", id_harmonics[harmonic]),
        ):
            actual = coefficient(key, harmonic)
            if not _close_complex(actual, expected, rel_tol=2.0e-6, abs_tol=1.0e-18):
                raise BundleImportError(
                    "workpoint Fourier coefficient %s[%d] is inconsistent for %s"
                    % (key, harmonic, point_name)
                )
    return {
        "samples": len(numeric),
        "raw_grid_t0_s": sum(inferred_origins) / len(inferred_origins),
        "grid_t0_spread_s": t0_spread,
        "product_max_abs_error": product_error,
    }


def _calculate_point(
    transfer, mdc, mdyn, carrier_phasor, offset_hz, reference, f0_hz
):
    try:
        calculated = shared_calculation.calculate_point(
            transfer,
            mdc,
            mdyn,
            abs(carrier_phasor),
            offset_hz,
            reference_pm_asd=reference,
            f0_hz=f0_hz,
            carrier_phasor=carrier_phasor,
        )
    except ValueError as exc:
        raise BundleImportError(str(exc))
    rows = []
    for row in calculated["harmonic_contributions"]:
        rows.append(
            {
                "k": row["k"],
                "Q_real_V_rtHz": row["Q_real"],
                "Q_imag_V_rtHz": row["Q_imag"],
                "Q_mag_V_rtHz": row["Q_mag"],
                "cum_real_V_rtHz": row["cum_real"],
                "cum_imag_V_rtHz": row["cum_imag"],
                "cum_mag_V_rtHz": row["cum_mag"],
            }
        )
    calculated["harmonic_contributions"] = rows
    return calculated


def recompute_dual_tb(canonical):
    """Recompute closure from already-parsed canonical two-TB rows."""

    from .thermal_budget import thermal_evidence
    if not isinstance(canonical, Mapping):
        raise ValidationError("canonical dual-TB data must be an object")
    vco_rows = list(canonical.get("vco_results", []))
    replica_rows = list(canonical.get("replica_results", []))
    workpoint_rows = list(canonical.get("workpoint", []))
    metadata = dict(canonical.get("metadata", {}))
    options = dict(canonical.get("options", {}))
    signoff = options.get("signoff", metadata.get("signoff", False))
    if not isinstance(signoff, bool):
        raise ValidationError("signoff must be a boolean")

    vco_by_point = _group_by_point(vco_rows, "vco_results")
    replica_by_point = _group_by_point(replica_rows, "replica_results")
    vco_points = set(vco_by_point)
    replica_points = set(replica_by_point)
    if vco_points != replica_points:
        raise BundleImportError(
            "VCO and replica point sets differ",
            details={
                "vco_only": sorted(vco_points - replica_points),
                "replica_only": sorted(replica_points - vco_points),
            },
        )
    workpoint_by_point = _group_by_point(workpoint_rows, "workpoint") if workpoint_rows else {}

    result = empty_result()
    result["dual_tb"] = {
        "format": metadata.get("format", "canonical-two-tb-v1"),
        "metadata": metadata,
        "vco_rows": len(vco_rows),
        "replica_rows": len(replica_rows),
        "workpoint_rows": len(workpoint_rows),
        "signoff": signoff,
    }

    if signoff:
        expected_metadata = {
            "format": "canonical-two-tb-v2",
            "run_kind": "signoff",
            "signoff": True,
            "canonical_noise_quantity": "ASD_A_per_sqrt_Hz",
            "source_noise_quantity": "PSD_A2_per_Hz",
            "psd_square_roots": 1,
            "current_direction": "drain_to_source",
            "carrier_amplitude": "differential_peak",
            "output_noise": "PM_SSB",
            "phase_selection": "HBNOISE_AM_PM_engineering_candidate",
            "hbac_role": "secondary_validation_only",
            "common_source_normalization": "Mdyn[k]=Dk*sqrt_Sxi",
            "time_origin": "shared_unshifted_VCO_period",
            "manual_waveform_shift": False,
            "device_count": 2,
            "second_device_model": "symmetric_independent",
            "independent_device_combination": "power",
            "isf_source": "carrier_referenced_signed_H_iq_v1",
            "isf_independent_of_noise_modulation": True,
        }
        missing_or_wrong = {}
        for key, expected in expected_metadata.items():
            if metadata.get(key) != expected:
                missing_or_wrong[key] = {"expected": expected, "actual": metadata.get(key)}
        if missing_or_wrong:
            raise BundleImportError(
                "signoff metadata is missing or inconsistent",
                details={"fields": missing_or_wrong},
            )
        for key in ("raw_t0_spread_s", "replica_drive_time_origin_s"):
            value = metadata.get(key)
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise BundleImportError("signoff metadata requires finite %s" % key)
            if not math.isfinite(value):
                raise BundleImportError("signoff metadata requires finite %s" % key)
        if abs(float(metadata["raw_t0_spread_s"])) > 1.0e-15:
            raise BundleImportError("raw PSF traces do not share one time origin")
        if abs(float(metadata["replica_drive_time_origin_s"])) > 1.0e-15:
            raise BundleImportError("replica drive does not share the VCO time origin")

    previous_by_corner = {}
    for point_name in sorted(vco_points):
        point_result = empty_point_result()
        vrows = vco_by_point[point_name]
        rrows = replica_by_point[point_name]
        _validate_point_sample_identity(
            point_name, vrows, rrows, workpoint_by_point.get(point_name, []), options, metadata
        )
        transfer = {}
        for index, row in enumerate(vrows):
            harmonic = _integer_harmonic(row, "%s.vco[%d]" % (point_name, index))
            if harmonic in transfer:
                raise BundleImportError("duplicate VCO harmonic %d for %s" % (harmonic, point_name))
            transfer[harmonic] = complex(
                _finite_number(row, "H_real_ohm", point_name),
                _finite_number(row, "H_imag_ohm", point_name),
            )

        id_harmonics = {}
        mdc = {}
        mdyn = {}
        am_asd = {}
        pm_asd = {}
        replica_rows_by_harmonic = {}
        chosen_signs = None
        chosen_score = None
        for index, row in enumerate(rrows):
            harmonic = _integer_harmonic(row, "%s.replica[%d]" % (point_name, index))
            if harmonic < 0 or harmonic in mdc:
                raise BundleImportError("invalid/duplicate replica harmonic for %s" % point_name)
            replica_rows_by_harmonic[harmonic] = row
            id_harmonics[harmonic] = complex(
                _finite_number(row, "Id_real_A", point_name),
                _finite_number(row, "Id_imag_A", point_name),
            )
            mdc[harmonic] = complex(
                _finite_number(row, "Mdc_real_A_rtHz", point_name),
                _finite_number(row, "Mdc_imag_A_rtHz", point_name),
            )
            if row.get("Mdyn_real_A_rtHz") not in (None, "") and row.get(
                "Mdyn_imag_A_rtHz"
            ) not in (None, ""):
                mdyn[harmonic] = complex(
                    _finite_number(row, "Mdyn_real_A_rtHz", point_name),
                    _finite_number(row, "Mdyn_imag_A_rtHz", point_name),
                )
            if harmonic > 0 and row.get("AM_A_rtHz") not in (None, ""):
                am_asd[harmonic] = _finite_number(row, "AM_A_rtHz", point_name)
                pm_asd[harmonic] = _finite_number(row, "PM_A_rtHz", point_name)
            chosen_signs = chosen_signs or row.get("phase_candidate")
            if chosen_score is None and row.get("phase_score") not in (None, ""):
                chosen_score = _finite_number(row, "phase_score", point_name)

        if 0 not in mdyn:
            zero_row = next(row for row in rrows if int(float(row["k"])) == 0)
            mdyn[0] = complex(_finite_number(zero_row, "USB_A_rtHz", point_name), 0.0)

        corner_key = str(vrows[0].get("corner", ""))
        candidates = _candidate_sets(
            id_harmonics,
            am_asd,
            pm_asd,
            mdc,
            previous=previous_by_corner.get(corner_key),
        )
        if not chosen_signs and candidates:
            chosen_signs = candidates[0]["signs"]
            chosen_score = candidates[0]["score"]
        chosen_candidate = next(
            (item for item in candidates if item["signs"] == chosen_signs), None
        )
        if chosen_candidate is not None:
            for harmonic, value in chosen_candidate["selected"].items():
                mdyn.setdefault(harmonic, value)
        missing_dynamic = sorted(set(mdc) - set(mdyn))
        if missing_dynamic:
            raise BundleImportError(
                "replica rows cannot construct Mdyn harmonics for %s: %s"
                % (point_name, missing_dynamic)
            )

        if signoff:
            declared_signs = set(
                str(row.get("phase_candidate", "")).strip()
                for row in rrows
                if str(row.get("phase_candidate", "")).strip()
            )
            if len(declared_signs) != 1 or chosen_candidate is None:
                raise BundleImportError(
                    "signoff requires one valid AM/PM phase candidate for %s"
                    % point_name
                )
            declared_scores = []
            for harmonic, row in replica_rows_by_harmonic.items():
                if row.get("phase_score") in (None, ""):
                    raise BundleImportError(
                        "signoff requires phase_score on every harmonic for %s"
                        % point_name
                    )
                declared_scores.append(_finite_number(row, "phase_score", point_name))
                for key in ("USB_A_rtHz", "HBAC_norm_mag", "HBAC_norm_phase_deg"):
                    if row.get(key) in (None, ""):
                        raise BundleImportError(
                            "signoff requires %s for harmonic %d in %s"
                            % (key, harmonic, point_name)
                        )
                    _finite_number(row, key, point_name)
                if harmonic > 0:
                    for key in ("AM_A_rtHz", "PM_A_rtHz", "LSB_A_rtHz"):
                        if row.get(key) in (None, ""):
                            raise BundleImportError(
                                "signoff requires %s for harmonic %d in %s"
                                % (key, harmonic, point_name)
                            )
                        if _finite_number(row, key, point_name) < 0.0:
                            raise BundleImportError(
                                "%s must be non-negative for %s" % (key, point_name)
                            )
                    if not _close_complex(
                        mdyn[harmonic], chosen_candidate["selected"][harmonic]
                    ):
                        raise BundleImportError(
                            "Mdyn[%d] does not match the selected AM/PM branch for %s"
                            % (harmonic, point_name)
                        )
            if any(
                not math.isclose(value, declared_scores[0], rel_tol=1.0e-9, abs_tol=1.0e-15)
                for value in declared_scores[1:]
            ):
                raise BundleImportError(
                    "phase_score must be identical on every harmonic for %s"
                    % point_name
                )
            if not math.isclose(
                declared_scores[0],
                float(chosen_candidate["score"]),
                rel_tol=1.0e-9,
                abs_tol=1.0e-15,
            ):
                raise BundleImportError(
                    "phase_score does not match the selected AM/PM candidate for %s"
                    % point_name
                )

        # If the producer supplies the preferred common-source contract, enforce
        # it as one normalization shared by every harmonic.  Legacy AM/PM-only
        # CSVs remain accepted, but a partial Dk/sqrt(Sxi) declaration is not.
        source_rows = [
            row for row in rrows
            if row.get("sqrt_Sxi_A_rtHz") not in (None, "")
        ]
        source_asd = None
        if source_rows:
            if len(source_rows) != len(rrows):
                raise BundleImportError(
                    "sqrt_Sxi_A_rtHz must be present on every harmonic for %s"
                    % point_name
                )
            source_values = [
                _finite_number(row, "sqrt_Sxi_A_rtHz", point_name)
                for row in source_rows
            ]
            source_asd = source_values[0]
            if source_asd <= 0.0 or any(
                not math.isclose(value, source_asd, rel_tol=1.0e-9, abs_tol=1.0e-30)
                for value in source_values[1:]
            ):
                raise BundleImportError(
                    "all harmonics must share one positive sqrt_Sxi_A_rtHz for %s"
                    % point_name
                )
            if signoff:
                zero_usb = _finite_number(
                    replica_rows_by_harmonic[0], "USB_A_rtHz", point_name
                )
                if not math.isclose(
                    zero_usb, source_asd, rel_tol=1.0e-9, abs_tol=1.0e-30
                ):
                    raise BundleImportError(
                        "k=0 USB ASD must equal the shared sqrt_Sxi for %s"
                        % point_name
                    )
            dk_rows = [
                row for row in rrows
                if row.get("Dk_real") not in (None, "")
                or row.get("Dk_imag") not in (None, "")
            ]
            if dk_rows and len(dk_rows) != len(rrows):
                raise BundleImportError(
                    "Dk_real/Dk_imag must be present on every harmonic for %s"
                    % point_name
                )
            if dk_rows:
                for harmonic, row in replica_rows_by_harmonic.items():
                    dk = complex(
                        _finite_number(row, "Dk_real", point_name),
                        _finite_number(row, "Dk_imag", point_name),
                    )
                    expected = dk * source_asd
                    if not _close_complex(expected, mdyn[harmonic]):
                        raise BundleImportError(
                            "Mdyn[%d] is inconsistent with the shared Dk*sqrt(Sxi) normalization for %s"
                            % (harmonic, point_name)
                        )
        elif signoff:
            raise BundleImportError(
                "signoff requires one shared sqrt_Sxi_A_rtHz and Dk for every harmonic in %s"
                % point_name
            )

        if signoff:
            for harmonic, row in replica_rows_by_harmonic.items():
                if row.get("Dk_real") in (None, "") or row.get("Dk_imag") in (None, ""):
                    raise BundleImportError(
                        "signoff requires Dk_real/Dk_imag for harmonic %d in %s"
                        % (harmonic, point_name)
                    )

        previous_by_corner[corner_key] = dict(mdyn)

        first = vrows[0]
        carrier_phasor = complex(
            _finite_number(first, "carrier_real_V", point_name),
            _finite_number(first, "carrier_imag_V", point_name),
        )
        carrier = abs(carrier_phasor)
        f0_hz = _finite_number(first, "f0_Hz", point_name)
        reference = _finite_number(first, "spectre_pm_asd_V_rtHz", point_name)
        total_noise_evidence = spectre_noise_evidence(first, point_name)
        thermal = thermal_evidence(first)
        for row in vrows[1:]:
            if spectre_noise_evidence(row, point_name) != total_noise_evidence:
                raise BundleImportError("inconsistent total noise evidence within scalar point " + point_name)
            if thermal_evidence(row) != thermal:
                raise BundleImportError("inconsistent thermal evidence within scalar point " + point_name)
        raw_offset = first.get("offset_Hz", first.get("offset_hz", options.get("offset_hz", metadata.get("offset_hz"))))
        try:
            offset_hz = float(raw_offset)
        except (TypeError, ValueError):
            raise BundleImportError("offset_Hz is required for %s" % point_name)
        if not math.isfinite(offset_hz) or offset_hz <= 0.0 or carrier <= 0.0 or f0_hz <= 0.0:
            raise BundleImportError("invalid carrier/frequency metadata for %s" % point_name)

        calculated = _calculate_point(
            transfer, mdc, mdyn, carrier_phasor, offset_hz, reference, f0_hz
        )
        result.setdefault("derived_diagnostics", {})[point_name] = {
            "label": "derived from canonical harmonics; diagnostic only",
            "curves": calculated.get("curves", {}),
        }
        single_dynamic_asd = calculated["methods"]["dynamic"]["pm_asd_V_rtHz"]
        signoff_pair_fields = (
            _validate_signoff_pair_fields(point_name, vrows) if signoff else {}
        )
        second_dynamic_asd = None
        if signoff:
            second_dynamic_asd = signoff_pair_fields[
                "second_device_predicted_pm_asd_V_rtHz"
            ]
        elif first.get("second_device_predicted_pm_asd_V_rtHz") not in (None, ""):
            second_dynamic_asd = _finite_number(
                first, "second_device_predicted_pm_asd_V_rtHz", point_name
            )
        elif metadata.get("second_device_model") == "symmetric_independent":
            second_dynamic_asd = single_dynamic_asd
        predicted_pair_asd = None
        spectre_pair_asd = None
        pair_error_db = None
        if second_dynamic_asd is not None:
            if second_dynamic_asd < 0.0:
                raise BundleImportError(
                    "second-device predicted ASD must be non-negative for %s"
                    % point_name
                )
            predicted_pair_asd = math.sqrt(
                single_dynamic_asd * single_dynamic_asd
                + second_dynamic_asd * second_dynamic_asd
            )
            if signoff:
                spectre_pair_asd = signoff_pair_fields[
                    "spectre_pair_pm_asd_V_rtHz"
                ]
                pair_error_db = _error_db(predicted_pair_asd, spectre_pair_asd)
            elif first.get("spectre_pair_pm_asd_V_rtHz") not in (None, ""):
                spectre_pair_asd = _finite_number(
                    first, "spectre_pair_pm_asd_V_rtHz", point_name
                )
                if spectre_pair_asd <= 0.0:
                    raise BundleImportError(
                        "Spectre pair ASD must be positive for %s" % point_name
                    )
                pair_error_db = _error_db(predicted_pair_asd, spectre_pair_asd)
        if signoff and (
            predicted_pair_asd is None
            or spectre_pair_asd is None
            or pair_error_db is None
            or not all(
                math.isfinite(value)
                for value in (predicted_pair_asd, spectre_pair_asd, pair_error_db)
            )
        ):
            raise BundleImportError(
                "signoff requires complete finite pair output/reference/error for %s"
                % point_name
            )
        workpoint_diagnostics = None
        point_workpoint = list(workpoint_by_point.get(point_name, []))
        if signoff:
            workpoint_diagnostics = _validate_signoff_workpoint(
                point_name,
                point_workpoint,
                f0_hz,
                mdc,
                mdyn,
                id_harmonics,
                transfer,
                carrier_phasor,
                offset_hz,
            )
        mdyn_rms = math.sqrt(
            abs(mdyn[0]) ** 2
            + 2.0 * sum(abs(value) ** 2 for harmonic, value in mdyn.items() if harmonic > 0)
        )
        summary = {
            "point": point_name,
            "operating_point_id": first.get("operating_point_id") or point_name,
            "operating_parameters": (
                (metadata.get("operating_points") or {}).get(first.get("operating_point_id") or point_name, {}).get("params", {})
            ),
            "corner": first.get("corner"),
            "f0_Hz": f0_hz,
            "offset_Hz": offset_hz,
            "carrier_peak_V": carrier,
            "chosen_signs": chosen_signs,
            "candidate_score": chosen_score,
            "mDC_cycle_mean_A_rtHz": mdc[0].real,
            "mDyn_rms_A_rtHz": mdyn_rms,
            "sqrt_Sxi_A_rtHz": source_asd,
            "shared_source_normalization": source_asd is not None,
            "signoff": signoff,
            "workpoint_grid_check": "PASS" if workpoint_diagnostics else "NOT_CHECKED",
            "workpoint_grid_t0_spread_s": (
                workpoint_diagnostics["grid_t0_spread_s"]
                if workpoint_diagnostics else None
            ),
            "uniform_delay_s": calculated["uniform_delay_s"],
            "dc_pm_asd_V_rtHz": calculated["methods"]["dc"]["pm_asd_V_rtHz"],
            "delay_pm_asd_V_rtHz": calculated["methods"]["delay"]["pm_asd_V_rtHz"],
            "dynamic_pm_asd_V_rtHz": calculated["methods"]["dynamic"]["pm_asd_V_rtHz"],
            "spectre_fn_pm_asd_V_rtHz": reference,
            "second_device_predicted_pm_asd_V_rtHz": second_dynamic_asd,
            "predicted_pair_pm_asd_V_rtHz": predicted_pair_asd,
            "spectre_pair_pm_asd_V_rtHz": spectre_pair_asd,
            "pair_error_dB": pair_error_db,
            "device_combination": (
                "independent_power_sum" if predicted_pair_asd is not None else None
            ),
            "dc_error_dB": calculated["methods"]["dc"]["error_dB"],
            "delay_error_dB": calculated["methods"]["delay"]["error_dB"],
            "dynamic_error_dB": calculated["methods"]["dynamic"]["error_dB"],
            "dc_dBc_Hz": calculated["methods"]["dc"]["dBc_Hz"],
            "delay_dBc_Hz": calculated["methods"]["delay"]["dBc_Hz"],
            "dynamic_dBc_Hz": calculated["methods"]["dynamic"]["dBc_Hz"],
            "physical_vdyn_real_V_rtHz": calculated["physical_vdyn"]["real"],
            "physical_vdyn_imag_V_rtHz": calculated["physical_vdyn"]["imag"],
            "heff_dynamic_exact_signed": calculated["heff_dynamic_exact"],
            "heff_carrier_i_signed": calculated["heff_carrier_i_signed"],
            "heff_carrier_q_signed": calculated["heff_carrier_q_signed"],
            "heff_branch_rss": calculated["heff_branch_rss"],
            "heff_from_physical_units": calculated["heff_from_physical_units"],
            "heff_from_carrier_projection": calculated[
                "heff_from_carrier_projection"
            ],
            "gamma_exact_equivalence_error_dB": calculated[
                "gamma_equivalence_error_dB"
            ],
            "gamma_to_output_magnitude_error_dB": calculated[
                "gamma_to_output_magnitude_error_dB"
            ],
            "gamma_reference": calculated["gamma_reference"],
        }
        summary.update(quality_fields(first))
        summary.update(total_noise_evidence)
        summary["thermal_noise"] = thermal_evidence(dict(summary, thermal_noise=thermal))
        result["summary"].append(summary)
        point_result["summary"].append(dict(summary))
        point_result["workpoint"].extend(
            dict(row) for row in point_workpoint
        )
        for row in calculated["harmonic_contributions"]:
            item = dict(row)
            harmonic = int(item["k"])
            mdc_value = mdc[harmonic]
            mdyn_value = mdyn[harmonic]
            correction = mdyn_value / mdc_value if mdc_value else 0j
            id_value = id_harmonics[harmonic]
            raw_replica = replica_rows_by_harmonic[harmonic]
            item.update(
                {
                    "Id_real_A": id_value.real,
                    "Id_imag_A": id_value.imag,
                    "Mdc_real_A_rtHz": mdc_value.real,
                    "Mdc_imag_A_rtHz": mdc_value.imag,
                    "Mdc_mag_A_rtHz": abs(mdc_value),
                    "Mdc_phase_deg": _phase_deg(mdc_value),
                    "Mdyn_real_A_rtHz": mdyn_value.real,
                    "Mdyn_imag_A_rtHz": mdyn_value.imag,
                    "Mdyn_mag_A_rtHz": abs(mdyn_value),
                    "Mdyn_phase_deg": _phase_deg(mdyn_value),
                    "A_k": abs(correction),
                    "delta_k_deg": math.degrees(_wrap_phase(cmath.phase(correction))) if correction else 0.0,
                    "Ck_real": correction.real,
                    "Ck_imag": correction.imag,
                    "Q_phase_deg": _phase_deg(
                        complex(item["Q_real_V_rtHz"], item["Q_imag_V_rtHz"])
                    ),
                    "sqrt_Sxi_A_rtHz": source_asd,
                    "phase_candidate": chosen_signs,
                    "phase_score": chosen_score,
                }
            )
            for key in (
                "AM_A_rtHz", "PM_A_rtHz", "USB_A_rtHz", "LSB_A_rtHz",
                "Dk_real", "Dk_imag", "HBAC_norm_mag", "HBAC_norm_phase_deg",
                "HBAC_vs_Mdyn_phase_deg",
            ):
                if raw_replica.get(key) not in (None, ""):
                    value = float(raw_replica[key])
                    if math.isfinite(value):
                        item[key] = value
            item["point"] = point_name
            point_result["harmonics"].append(item)
        for rank, candidate in enumerate(candidates, 1):
            point_result["phase_candidates"].append(
                {
                    "point": point_name,
                    "rank": rank,
                    "chosen": candidate["signs"] == chosen_signs,
                    "signs": candidate["signs"],
                    "score": candidate["score"],
                    "phase_cost": candidate["phase_cost"],
                    "delay_cost": candidate["delay_cost"],
                    "continuity_cost": candidate["continuity_cost"],
                }
            )
        result["points"][point_name] = point_result

    return normalize_result(enrich_result(result))


def import_dual_tb_bundle(payload):
    """Read canonical VCO/replica CSVs and recompute their closure."""

    if isinstance(payload, (str, bytes)) or hasattr(payload, "__fspath__"):
        options = {"path": payload}
    elif isinstance(payload, Mapping):
        options = dict(payload)
    else:
        raise ValidationError("dual-TB payload must be an object or directory path")
    encoding = options.get("encoding", "utf-8-sig")
    if not isinstance(encoding, str) or not encoding:
        raise ValidationError("encoding must be a non-empty string")
    vco_path, replica_path, workpoint_path, metadata_path = _resolve_files(options)
    canonical = {
        "vco_results": _read_csv(vco_path, _VCO_REQUIRED, encoding),
        "replica_results": _read_csv(replica_path, _REPLICA_REQUIRED, encoding),
        "workpoint": (
            _read_csv(workpoint_path, frozenset(("point",)), encoding)
            if workpoint_path is not None
            else []
        ),
        "metadata": _read_metadata(metadata_path, encoding),
        "options": options,
    }
    result = recompute_dual_tb(canonical)
    result["dual_tb"]["vco_path"] = vco_path
    result["dual_tb"]["replica_path"] = replica_path
    result["dual_tb"]["workpoint_path"] = workpoint_path
    result["dual_tb"]["metadata_path"] = metadata_path
    return normalize_result(result)


class DualTBImporter(object):
    def import_bundle(self, payload):
        return import_dual_tb_bundle(payload)
