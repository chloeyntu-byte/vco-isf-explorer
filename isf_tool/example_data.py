"""Synthetic ideal-LC replay bundle used by the examples and the "Load example" button.

Everything here is an analytic or fictional model, not simulator output:

* an ideal parallel LC oscillator with a sinusoidal carrier,
* a transfer function ``H[k]`` that is purely first-harmonic for the ideal
  point (so the ISF is a pure sine) and gains a small second-harmonic part for
  the two "asymmetric" points,
* a cyclostationary current-noise modulation ``D[k]`` that decays with k,
* a reference phase noise defined as the model prediction shifted by a fixed
  dB offset, so the error plot has something to show.

``build(directory)`` writes ``vco_results.csv``, ``replica_results.csv``,
``workpoint.csv`` and ``metadata.json`` (canonical two-TB replay format).
"""
from __future__ import annotations

import csv
import json
import math
import os

from . import calculation

F0_HZ = 2.4e9
OFFSETS_HZ = (1.0e3, 3.0e3, 1.0e4, 3.0e4, 1.0e5, 3.0e5, 1.0e6)
CORNER_HZ = 2.0e5  # fictional flicker corner of the white+1/f current-noise source
MAX_K = 4
SAMPLES = 64
R_TANK_OHM = 600.0
CARRIER_V = complex(0.9, 0.0)
WHITE_ASD = 2.0e-10  # A/sqrt(Hz), fictional white current-noise level

# name, second-harmonic content of the transfer, dB shift of the reference.
POINTS = (
    ("lc_ideal", 0.0, -0.4),
    ("lc_mild_asym", 0.15, 0.6),
    ("lc_strong_asym", 0.40, 1.5),
)


def _periodic(coefficients, phase):
    value = complex(coefficients.get(0, 0)).real
    for k in sorted(coefficients):
        if k > 0:
            value += 2.0 * (complex(coefficients[k]) * complex(math.cos(k * phase), math.sin(k * phase))).real
    return value


def _point(name, op_id, second, shift_db, offset):
    transfer = {0: 0j}
    for k in range(1, MAX_K + 1):
        if k == 1:
            h = complex(0.0, -R_TANK_OHM / 2.0)
        elif k == 2:
            h = complex(0.0, -R_TANK_OHM / 2.0) * second
        else:
            h = 0j
        transfer[k] = h
        transfer[-k] = h.conjugate()
    # D[k]: modulation of the noise source over one period (decaying harmonics).
    dyn_norm = {0: complex(1.0, 0.0)}
    for k in range(1, MAX_K + 1):
        phase = 0.35 * k
        dyn_norm[k] = 0.45 * math.exp(-0.7 * k) * complex(math.cos(phase), math.sin(phase))
    source_asd = WHITE_ASD * math.sqrt(1.0 + CORNER_HZ / offset)
    mdyn = {k: v * source_asd for k, v in dyn_norm.items()}
    mdc = dict(mdyn)
    bias = 4.0e-3
    id_h = {0: complex(bias, 0.0)}
    for k in range(1, MAX_K + 1):
        id_h[k] = complex(bias * 0.25 * 0.5 ** (k - 1), 0.0)

    pred = calculation.calculate_point(
        transfer, mdc, mdyn, abs(CARRIER_V), offset, f0_hz=F0_HZ, carrier_phasor=CARRIER_V,
    )["methods"]["dynamic"]["pm_asd_V_rtHz"]
    reference = pred / (10.0 ** (shift_db / 20.0))

    vco, replica, work = [], [], []
    for k in range(-MAX_K, MAX_K + 1):
        vco.append({
            "point": name, "operating_point_id": op_id, "k": k, "corner": "typ", "f0_Hz": F0_HZ, "offset_Hz": offset,
            "carrier_real_V": CARRIER_V.real, "carrier_imag_V": CARRIER_V.imag,
            "H_real_ohm": transfer[k].real, "H_imag_ohm": transfer[k].imag,
            "spectre_pm_asd_V_rtHz": reference,
            "second_device_predicted_pm_asd_V_rtHz": pred,
            "spectre_pair_pm_asd_V_rtHz": math.sqrt(2.0) * reference,
        })
    for k in range(MAX_K + 1):
        d = mdyn[k]
        replica.append({
            "point": name, "operating_point_id": op_id, "offset_Hz": offset, "k": k, "Id_real_A": id_h[k].real, "Id_imag_A": id_h[k].imag,
            "Mdc_real_A_rtHz": mdc[k].real, "Mdc_imag_A_rtHz": mdc[k].imag,
            "AM_A_rtHz": d.real if k else 0.0, "PM_A_rtHz": d.imag if k else 0.0,
            "USB_A_rtHz": source_asd if k == 0 else abs(d), "LSB_A_rtHz": 0.0 if k == 0 else abs(d),
            "Dk_real": dyn_norm[k].real, "Dk_imag": dyn_norm[k].imag,
            "sqrt_Sxi_A_rtHz": source_asd, "Mdyn_real_A_rtHz": d.real, "Mdyn_imag_A_rtHz": d.imag,
            "HBAC_norm_mag": abs(dyn_norm[k]), "HBAC_norm_phase_deg": math.degrees(math.atan2(d.imag, d.real)),
            "phase_candidate": "+" * MAX_K, "phase_score": 0.0,
        })
    gamma = calculation.carrier_referenced_gamma_branches(transfer, CARRIER_V, offset, MAX_K)
    for s in range(SAMPLES + 1):
        ph = 2.0 * math.pi * s / SAMPLES
        isf_i, isf_q = _periodic(gamma["i"], ph), _periodic(gamma["q"], ph)
        dyn = _periodic(mdyn, ph)
        cur = _periodic(id_h, ph)
        work.append({
            "point": name, "operating_point_id": op_id, "offset_Hz": offset, "sample": s, "time_s": ph / (2.0 * math.pi * F0_HZ), "wt_rad": ph,
            "VG_V": 0.60 + 0.30 * math.cos(ph), "VD_V": 0.90 + 0.30 * math.sin(ph), "VS_V": 0.0, "VB_V": 0.0,
            "ID_main_A": cur * 0.98, "ID_replica_A": cur,
            "m_DC_A_rtHz": _periodic(mdc, ph), "m_dyn_A_rtHz": dyn,
            "isf": isf_i, "product": isf_i * dyn, "isf_i": isf_i, "isf_q": isf_q,
            "product_i": isf_i * dyn, "product_q": isf_q * dyn,
        })
    return vco, replica, work


def _write(path, rows):
    fields = list(rows[0])
    with open(path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build(out):
    os.makedirs(out, exist_ok=True)
    vco, replica, work = [], [], []
    for op_id, second, shift in POINTS:
        for offset in OFFSETS_HZ:
            # One canonical "point" is one operating point at one offset.
            a, b, c = _point("%s@%g" % (op_id, offset), op_id, second, shift, offset)
            vco += a
            replica += b
            work += c
    _write(os.path.join(out, "vco_results.csv"), vco)
    _write(os.path.join(out, "replica_results.csv"), replica)
    _write(os.path.join(out, "workpoint.csv"), work)
    metadata = {
        "format": "canonical-two-tb-v2", "run_kind": "replay", "signoff": False,
        "point_count": len(POINTS) * len(OFFSETS_HZ),
        "canonical_noise_quantity": "ASD_A_per_sqrt_Hz", "source_noise_quantity": "PSD_A2_per_Hz",
        "psd_square_roots": 1, "current_direction": "drain_to_source", "carrier_amplitude": "differential_peak",
        "output_noise": "PM_SSB", "phase_selection": "HBNOISE_AM_PM_engineering_candidate",
        "hbac_role": "secondary_validation_only", "common_source_normalization": "Mdyn[k]=Dk*sqrt_Sxi",
        "time_origin": "shared_unshifted_VCO_period", "raw_t0_spread_s": 0.0,
        "replica_drive_time_origin_s": 0.0, "manual_waveform_shift": False,
        "isf_source": "carrier_referenced_signed_H_iq_v1", "isf_independent_of_noise_modulation": True,
        "fixture_origin": "analytic_ideal_lc_synthetic",
        "device_count": 2, "second_device_model": "symmetric_independent",
        "independent_device_combination": "power",
    }
    with open(os.path.join(out, "metadata.json"), "w", encoding="utf-8") as stream:
        json.dump(metadata, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return out
