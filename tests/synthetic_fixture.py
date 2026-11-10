"""Deterministic, fully synthetic fixtures for portable ISF tests.

The numbers in this module are deliberately simple engineering examples.  They
do not originate from a PDK, a Cadence database, or a measured design.
"""

from __future__ import unicode_literals

import copy
import csv
import io
import json
import math
import os

from isf_tool import calculation
from isf_tool.core.dual_tb import recompute_dual_tb


POINT_NAMES = (
    "point_01_PDK_A_typ",
    "point_02_PDK_A_slow",
    "point_03_PDK_A_fast",
    "point_04_PDK_B_typ",
    "point_05_PDK_B_slow",
    "point_06_PDK_B_fast",
)
ERROR_TARGETS_DB = (-2.0, -1.0, -0.5, 0.5, 1.0, 2.0)
SAMPLES_PER_PERIOD = 64
MAX_HARMONIC = 3
OFFSET_HZ = 10000.0


def _periodic_value(coefficients, phase):
    value = complex(coefficients[0]).real
    for harmonic in sorted(coefficients):
        if harmonic > 0:
            value += 2.0 * (
                complex(coefficients[harmonic])
                * complex(
                    math.cos(harmonic * phase),
                    math.sin(harmonic * phase),
                )
            ).real
    return value


def _point_data(index, point_name, error_target_db):
    scale = 1.0 + 0.04 * index
    corner = point_name.rsplit("_", 1)[-1]
    f0_hz = 1.0e9 * (1.0 + 0.02 * index)
    carrier = complex(1.0 + 0.03 * index, 0.25 - 0.01 * index)

    positive_transfer = {
        0: complex(0.8e5 * scale, 0.0),
        1: complex(1.1e5 * scale, -0.35e5 * scale),
        2: complex(-0.45e5 * scale, 0.22e5 * scale),
        3: complex(0.18e5 * scale, 0.08e5 * scale),
    }
    transfer = {0: positive_transfer[0]}
    for harmonic in range(1, MAX_HARMONIC + 1):
        transfer[harmonic] = positive_transfer[harmonic]
        transfer[-harmonic] = positive_transfer[harmonic].conjugate()

    source_asd = 2.0e-10 * (1.0 + 0.03 * index)
    normalized_dynamic = {
        0: complex(1.0, 0.0),
        1: complex(0.20, 0.10),
        2: complex(0.08, 0.04),
        3: complex(0.03, 0.015),
    }
    mdyn = {
        harmonic: value * source_asd
        for harmonic, value in normalized_dynamic.items()
    }
    # Matching DC/dynamic phases makes the declared +++ AM/PM branch an
    # unambiguous zero-cost candidate without embedding simulator output.
    mdc = dict(mdyn)
    id_harmonics = {
        0: complex(5.0e-3 * scale, 0.0),
        1: complex(8.0e-4 * scale, 0.0),
        2: complex(2.5e-4 * scale, 0.0),
        3: complex(8.0e-5 * scale, 0.0),
    }

    preliminary = calculation.calculate_point(
        transfer,
        mdc,
        mdyn,
        abs(carrier),
        OFFSET_HZ,
        f0_hz=f0_hz,
        carrier_phasor=carrier,
    )
    predicted = preliminary["methods"]["dynamic"]["pm_asd_V_rtHz"]
    reference = predicted / (10.0 ** (error_target_db / 20.0))

    vco_rows = []
    for harmonic in range(-MAX_HARMONIC, MAX_HARMONIC + 1):
        value = transfer[harmonic]
        vco_rows.append(
            {
                "point": point_name,
                "k": harmonic,
                "corner": corner,
                "f0_Hz": f0_hz,
                "offset_Hz": OFFSET_HZ,
                "carrier_real_V": carrier.real,
                "carrier_imag_V": carrier.imag,
                "H_real_ohm": value.real,
                "H_imag_ohm": value.imag,
                "spectre_pm_asd_V_rtHz": reference,
            }
        )

    replica_rows = []
    for harmonic in range(MAX_HARMONIC + 1):
        dynamic = mdyn[harmonic]
        magnitude = abs(dynamic)
        replica_rows.append(
            {
                "point": point_name,
                "k": harmonic,
                "Id_real_A": id_harmonics[harmonic].real,
                "Id_imag_A": id_harmonics[harmonic].imag,
                "Mdc_real_A_rtHz": mdc[harmonic].real,
                "Mdc_imag_A_rtHz": mdc[harmonic].imag,
                "AM_A_rtHz": dynamic.real if harmonic > 0 else 0.0,
                "PM_A_rtHz": dynamic.imag if harmonic > 0 else 0.0,
                "USB_A_rtHz": source_asd if harmonic == 0 else magnitude,
                "LSB_A_rtHz": 0.0 if harmonic == 0 else magnitude,
                "Dk_real": normalized_dynamic[harmonic].real,
                "Dk_imag": normalized_dynamic[harmonic].imag,
                "sqrt_Sxi_A_rtHz": source_asd,
                "Mdyn_real_A_rtHz": dynamic.real,
                "Mdyn_imag_A_rtHz": dynamic.imag,
                "HBAC_norm_mag": abs(normalized_dynamic[harmonic]),
                "HBAC_norm_phase_deg": math.degrees(
                    math.atan2(dynamic.imag, dynamic.real)
                ),
                "phase_candidate": "+" * MAX_HARMONIC,
                "phase_score": 0.0,
            }
        )

    gamma = calculation.carrier_referenced_gamma_branches(
        transfer, carrier, OFFSET_HZ, MAX_HARMONIC
    )
    workpoint_rows = []
    for sample in range(SAMPLES_PER_PERIOD + 1):
        phase = 2.0 * math.pi * sample / SAMPLES_PER_PERIOD
        time_s = phase / (2.0 * math.pi * f0_hz)
        dynamic = _periodic_value(mdyn, phase)
        isf_i = _periodic_value(gamma["i"], phase)
        isf_q = _periodic_value(gamma["q"], phase)
        replica_current = _periodic_value(id_harmonics, phase)
        workpoint_rows.append(
            {
                "point": point_name,
                "sample": sample,
                "time_s": time_s,
                "wt_rad": phase,
                "VG_V": 0.60 + 0.10 * math.cos(phase),
                "VD_V": 0.70 + 0.05 * math.sin(phase),
                "VS_V": 0.0,
                "VB_V": 0.0,
                "ID_main_A": replica_current * 0.98,
                "ID_replica_A": replica_current,
                "m_DC_A_rtHz": _periodic_value(mdc, phase),
                "m_dyn_A_rtHz": dynamic,
                "isf": isf_i,
                "product": isf_i * dynamic,
                "isf_i": isf_i,
                "isf_q": isf_q,
                "product_i": isf_i * dynamic,
                "product_q": isf_q * dynamic,
            }
        )
    return vco_rows, replica_rows, workpoint_rows


def make_canonical(signoff=True, point_count=6):
    """Return a fresh canonical-v2 mapping for up to six fictional points."""

    if point_count < 1 or point_count > len(POINT_NAMES):
        raise ValueError("point_count must be between 1 and 6")
    vco_rows = []
    replica_rows = []
    workpoint_rows = []
    for index, (point_name, target) in enumerate(
        zip(POINT_NAMES[:point_count], ERROR_TARGETS_DB[:point_count])
    ):
        vco, replica, workpoint = _point_data(index, point_name, target)
        vco_rows.extend(vco)
        replica_rows.extend(replica)
        workpoint_rows.extend(workpoint)

    metadata = {
        "format": "canonical-two-tb-v2",
        "run_kind": "signoff" if signoff else "replay",
        "signoff": bool(signoff),
        "point_count": point_count,
        "offset_hz": OFFSET_HZ,
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
        "raw_t0_spread_s": 0.0,
        "replica_drive_time_origin_s": 0.0,
        "manual_waveform_shift": False,
        "isf_source": "carrier_referenced_signed_H_iq_v1",
        "isf_independent_of_noise_modulation": True,
        "fixture_origin": "programmatic_fictional_unit_test",
    }
    if signoff:
        metadata.update(
            {
                "device_count": 2,
                "second_device_model": "symmetric_independent",
                "independent_device_combination": "power",
            }
        )

    canonical = {
        "vco_results": vco_rows,
        "replica_results": replica_rows,
        "workpoint": workpoint_rows,
        "metadata": metadata,
        "options": {"signoff": bool(signoff)},
    }
    if signoff:
        replay = copy.deepcopy(canonical)
        replay["options"] = {"signoff": False}
        baseline = recompute_dual_tb(replay)
        singles = {
            row["point"]: row["dynamic_pm_asd_V_rtHz"]
            for row in baseline["summary"]
        }
        references = {
            row["point"]: float(row["spectre_pm_asd_V_rtHz"])
            for row in vco_rows
        }
        for row in vco_rows:
            point = row["point"]
            row["second_device_predicted_pm_asd_V_rtHz"] = singles[point]
            row["spectre_pair_pm_asd_V_rtHz"] = math.sqrt(2.0) * references[point]
    return canonical


def _fieldnames(rows):
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    return fields


def _write_csv(path, rows):
    rows = list(rows)
    if not rows:
        raise ValueError("cannot write an empty synthetic CSV")
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    with io.open(path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_fieldnames(rows))
        writer.writeheader()
        writer.writerows(rows)


def write_canonical_bundle(path, signoff=True, point_count=6, canonical=None):
    """Write the canonical CSV/JSON contract and return its absolute path."""

    path = os.path.abspath(os.fspath(path))
    if not os.path.isdir(path):
        os.makedirs(path)
    value = copy.deepcopy(
        canonical
        if canonical is not None
        else make_canonical(signoff=signoff, point_count=point_count)
    )
    _write_csv(os.path.join(path, "vco_results.csv"), value["vco_results"])
    _write_csv(
        os.path.join(path, "replica_results.csv"), value["replica_results"]
    )
    _write_csv(os.path.join(path, "workpoint.csv"), value["workpoint"])
    with io.open(os.path.join(path, "metadata.json"), "w", encoding="utf-8") as stream:
        json.dump(value["metadata"], stream, indent=2, sort_keys=True)
        stream.write("\n")
    return path


def write_closure_bundle(path, point_count=6):
    """Write canonical input plus the equivalent audited closure CSV tables."""

    path = os.path.abspath(os.fspath(path))
    if not os.path.isdir(path):
        os.makedirs(path)
    canonical = make_canonical(signoff=True, point_count=point_count)
    canonical_path = os.path.join(path, "canonical")
    write_canonical_bundle(canonical_path, canonical=canonical)
    result = recompute_dual_tb(copy.deepcopy(canonical))
    _write_csv(os.path.join(path, "summary.csv"), result["summary"])

    all_harmonics = []
    all_candidates = []
    for point_name in POINT_NAMES[:point_count]:
        tables = result["points"][point_name]
        point_path = os.path.join(path, point_name)
        _write_csv(
            os.path.join(point_path, "device_workpoint.csv"),
            tables["workpoint"],
        )
        _write_csv(
            os.path.join(point_path, "harmonic_closure.csv"),
            tables["harmonics"],
        )
        _write_csv(
            os.path.join(point_path, "phase_candidates.csv"),
            tables["phase_candidates"],
        )
        with io.open(
            os.path.join(point_path, "summary.json"), "w", encoding="utf-8"
        ) as stream:
            json.dump(tables["summary"][0], stream, indent=2, sort_keys=True)
            stream.write("\n")
        all_harmonics.extend(tables["harmonics"])
        all_candidates.extend(tables["phase_candidates"])

    # These duplicates intentionally exercise the importer's aggregate-table
    # suppression when authoritative per-point detail files are present.
    _write_csv(os.path.join(path, "harmonics_all.csv"), all_harmonics)
    _write_csv(os.path.join(path, "phase_candidates_all.csv"), all_candidates)
    with io.open(os.path.join(path, "metadata.json"), "w", encoding="utf-8") as stream:
        json.dump(canonical["metadata"], stream, indent=2, sort_keys=True)
        stream.write("\n")
    return path
