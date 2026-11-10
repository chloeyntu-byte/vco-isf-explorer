"""Independent white/cyclostationary noise math; the flicker solver is unchanged.

Spectra are one-sided. Raw output-voltage H is not automatically pure PM.
``dBc_Hz`` is a carrier-normalized diagnostic; ``phase_noise_dBc_Hz`` requires
an explicitly independently validated signed-output projection contract.
"""

from __future__ import annotations

import cmath
import math
from collections.abc import Mapping

from .calculation import dBc_from_pm_asd


def _real(value, label, *, nonnegative=False, positive=False):
    if isinstance(value, bool):
        raise ValueError(label + " must be finite real")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(label + " must be finite real") from exc
    if not math.isfinite(value) or (nonnegative and value < 0) or (positive and value <= 0):
        raise ValueError(label + " is outside its finite physical domain")
    return value


def _complex(value, label):
    if isinstance(value, bool):
        raise ValueError(label + " must be finite complex")
    try:
        value = complex(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(label + " must be finite complex") from exc
    if not math.isfinite(value.real) or not math.isfinite(value.imag):
        raise ValueError(label + " must be finite complex")
    return value


def _integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(label + " must be an integer")
    return value


def _coefficients(values, label, *, contiguous=True):
    if not isinstance(values, Mapping) or not values:
        raise ValueError(label + " must be a nonempty harmonic mapping")
    output = {_integer(k, label + " index"): _complex(v, label) for k, v in values.items()}
    if contiguous and set(output) != set(range(min(output), max(output) + 1)):
        raise ValueError(label + " has missing harmonic bands")
    return output


def _units(source_psd_units, transfer_units):
    if (source_psd_units, transfer_units) not in (("A^2/Hz", "V/A"), ("V^2/Hz", "V/V")):
        raise ValueError("source PSD and transfer units must produce V^2/Hz")


def _contract(quantity, contract):
    if quantity not in ("output_voltage_ssb", "PM_SSB", "AM_SSB"):
        raise ValueError("unknown transfer_quantity")
    if quantity == "PM_SSB":
        if not isinstance(contract, Mapping) or isinstance(contract.get("schema_version"), bool) or contract.get("schema_version") != 1:
            raise ValueError("PM_SSB requires a versioned projection contract")
        if contract.get("convention") != "same_input_signed_output_peak_phasor_v1" or contract.get("validated") is not True or not contract.get("validation_evidence"):
            raise ValueError("PM_SSB requires independent projection validation")
    return dict(contract or {})


def _result(psd, *, source_id, carrier_peak, transfer_quantity, pm_contract, method):
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id must be nonempty")
    psd = _real(psd, "output PSD", nonnegative=True)
    contract = _contract(transfer_quantity, pm_contract)
    carrier = None if carrier_peak is None else _real(carrier_peak, "carrier_peak", positive=True)
    asd = math.sqrt(psd)
    dbc = dBc_from_pm_asd(asd, carrier) if carrier is not None else None
    return {"schema_version": 1, "method": method, "source_id": source_id,
            "noise_quantity": transfer_quantity, "psd_convention": "one_sided",
            "total_psd_V2_Hz": psd, "total_asd_V_rtHz": asd,
            "carrier_peak_V": carrier, "dBc_Hz": dbc,
            "phase_noise_dBc_Hz": dbc if transfer_quantity == "PM_SSB" else None,
            "pm_projection_contract": contract}


def calculate_white_thermal(transfer, amplitude_coefficients, *, carrier_peak=None,
                            source_id="source", input_bands=None,
                            max_input_harmonic=None,
                            source_psd_units="A^2/Hz", transfer_units="V/A",
                            transfer_quantity="output_voltage_ssb", pm_contract=None):
    """B_m=sum_k H_k*a_(k-m), followed by independent m power summation.

    a is the complete signed Fourier mapping of the real periodic sqrt(PSD).
    Finite H/a supports define the approximation; explicit input_bands cannot
    silently omit convolution edges. An explicit max_input_harmonic requests
    a finite input-band budget and reports its omitted power separately.
    Paths WITHIN each m remain coherent.
    """
    _units(source_psd_units, transfer_units)
    h = _coefficients(transfer, "transfer")
    a = _coefficients(amplitude_coefficients, "amplitude")
    if 0 not in a or set(a) != set(range(-max(a), max(a) + 1)):
        raise ValueError("real amplitude needs complete symmetric signed harmonics including zero")
    tolerance = 1e-10 * max(abs(v) for v in a.values())
    if abs(a[0].imag) > tolerance or any(abs(a[-k] - a[k].conjugate()) > tolerance for k in a):
        raise ValueError("real amplitude Fourier coefficients lack conjugate symmetry")
    required = {k - n for k in h for n in a}
    if max_input_harmonic is not None:
        max_input_harmonic = _integer(max_input_harmonic, "max_input_harmonic")
        if max_input_harmonic < 0 or input_bands is not None:
            raise ValueError("explicit input harmonic limit must be nonnegative and exclusive of input_bands")
    bands = sorted(required) if input_bands is None else [_integer(k, "input band") for k in input_bands]
    if len(bands) != len(set(bands)) or not required.issubset(bands):
        raise ValueError("input_bands contains duplicates or omits convolution bands")
    rows = []
    for m in sorted(bands):
        paths = [(k, h[k] * a[k - m]) for k in sorted(h) if k - m in a]
        value = sum((value for _, value in paths), 0j)
        rows.append({"input_band": m, "response_real_V_rtHz": value.real,
                     "response_imag_V_rtHz": value.imag, "psd_V2_Hz": abs(value) ** 2,
                     "coherent_paths": [{"transfer_band": k, "amplitude_band": k - m,
                         "real_V_rtHz": v.real, "imag_V_rtHz": v.imag} for k, v in paths]})
    full_power = math.fsum(row["psd_V2_Hz"] for row in rows)
    omitted = 0.0
    if max_input_harmonic is not None:
        omitted = math.fsum(row["psd_V2_Hz"] for row in rows if abs(row["input_band"]) > max_input_harmonic)
        rows = [row for row in rows if abs(row["input_band"]) <= max_input_harmonic]
    out = _result(math.fsum(row["psd_V2_Hz"] for row in rows), source_id=source_id,
                  carrier_peak=carrier_peak, transfer_quantity=transfer_quantity,
                  pm_contract=pm_contract, method="scalar_memoryless_white_fourier_v1")
    out.update(input_band_contributions=rows, transfer_bands=sorted(h), amplitude_bands=sorted(a),
               independent_combination="input_band_power_sum", within_band_combination="coherent_paths",
               source_psd_units=source_psd_units, transfer_units=transfer_units)
    if max_input_harmonic is not None:
        out["input_band_truncation"] = {"max_harmonic": max_input_harmonic,
            "omitted_psd_V2_Hz": omitted, "untruncated_psd_V2_Hz": full_power}
    return out


def _periodic_samples(times, values, period, label, *, complex_values=False):
    period = _real(period, "period", positive=True)
    times = [_real(t, "sample time") for t in times]
    values = [(_complex(v, label) if complex_values else _real(v, label, nonnegative=True)) for v in values]
    if len(times) < 2 or len(times) != len(values) or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("sample arrays must match and times must strictly increase")
    if not math.isclose(times[-1] - times[0], period, rel_tol=1e-9, abs_tol=0):
        raise ValueError("samples must cover one complete period including closure")
    if abs(values[-1] - values[0]) > max(abs(v) for v in values) * 1e-9:
        raise ValueError(label + " does not close periodically")
    return times, values, period


def time_weighted_white_psd(times, h_values, source_psd, *, period, carrier_peak=None,
                            source_id="source", source_psd_units="A^2/Hz", transfer_units="V/A",
                            transfer_quantity="output_voltage_ssb", pm_contract=None):
    """Nonuniform trapezoidal mean |h(t)|^2 S(t); no extra offset integration."""
    _units(source_psd_units, transfer_units)
    times, h, period = _periodic_samples(list(times), h_values, period, "kernel", complex_values=True)
    _, density, _ = _periodic_samples(times, source_psd, period, "source PSD")
    values = [abs(v) ** 2 * s for v, s in zip(h, density)]
    power = math.fsum((t1 - t0) * (v0 + v1) / (2 * period)
                     for t0, t1, v0, v1 in zip(times, times[1:], values, values[1:]))
    out = _result(power, source_id=source_id, carrier_peak=carrier_peak,
                  transfer_quantity=transfer_quantity, pm_contract=pm_contract,
                  method="scalar_memoryless_white_time_v1")
    out.update(period=period, sample_count=len(times), quadrature="nonuniform_trapezoidal",
               integrand=[{"time": t, "psd_V2_Hz": v} for t, v in zip(times, values)],
               source_psd_units=source_psd_units, transfer_units=transfer_units)
    return out


def fourier_amplitude_from_psd(times, source_psd, *, period, max_harmonic):
    """Trapezoidal coefficients of sqrt(PSD), including both signed orders."""
    max_harmonic = _integer(max_harmonic, "max_harmonic")
    if max_harmonic < 0:
        raise ValueError("max_harmonic must be non-negative")
    times, density, period = _periodic_samples(times, source_psd, period, "source PSD")
    result = {}
    for k in range(max_harmonic + 1):
        values = [math.sqrt(s) * cmath.exp(-2j * math.pi * k * (t - times[0]) / period) for t, s in zip(times, density)]
        integral = sum(((t1 - t0) * (v0 + v1) / (2 * period)
                        for t0, t1, v0, v1 in zip(times, times[1:], values, values[1:])), 0j)
        result[k] = complex(integral.real, 0) if k == 0 else integral
        if k:
            result[-k] = result[k].conjugate()
    return result


def project_am_pm(upper, negative_lower, carrier_phasor, *, validated=False, validation_evidence=None):
    """Same-input-band responses to +f0+df / -f0+df; NOT positive LSB.

    Pure algebra does not validate a simulator's signed phasor convention.
    """
    hp = _coefficients(upper, "upper response", contiguous=False)
    hm = _coefficients(negative_lower, "negative-lower response", contiguous=False)
    if set(hp) != set(hm):
        raise ValueError("PM projection requires matching input bands in both responses")
    carrier = _complex(carrier_phasor, "carrier phasor")
    if not carrier:
        raise ValueError("carrier phasor must be nonzero")
    if not isinstance(validated, bool) or (validated and not validation_evidence):
        raise ValueError("validated projection requires explicit independent evidence")
    r = carrier.conjugate() / abs(carrier)
    return {"am_transfer": {k: (r * hp[k] + r.conjugate() * hm[k]) / 2 for k in sorted(hp)},
            "pm_transfer": {k: (r * hp[k] - r.conjugate() * hm[k]) / (2j) for k in sorted(hp)},
            "contract": {"schema_version": 1, "convention": "same_input_signed_output_peak_phasor_v1",
                         "validated": validated, "validation_evidence": validation_evidence,
                         "positive_output": "+f0+offset", "negative_output": "-f0+offset",
                         "carrier_peak_V": abs(carrier)}}


def calculate_covariance_thermal(transfer, covariance, *, bands=None, carrier_peak=None,
                                 source_id="source", source_psd_units="A^2/Hz", transfer_units="V/A",
                                 transfer_quantity="output_voltage_ssb", pm_contract=None):
    """H^T C H*; retain every cyclic/port covariance, including explicit zeros.

    Hermitian PSD factorization yields auditable independent latent-mode
    powers. Latent modes must not be mislabeled physical source bands.
    """
    _units(source_psd_units, transfer_units)
    h = _coefficients(transfer, "transfer", contiguous=False)
    ordered = sorted(h) if bands is None else [_integer(k, "band") for k in bands]
    if len(ordered) != len(set(ordered)) or set(ordered) != set(h):
        raise ValueError("covariance bands must exactly match transfer bands")
    if not isinstance(covariance, Mapping) or set(covariance) != {(k, l) for k in ordered for l in ordered}:
        raise ValueError("full covariance is required; missing correlations cannot be assumed zero")
    c = {key: _complex(value, "covariance") for key, value in covariance.items()}
    tolerance = 1e-11 * max(abs(v) for v in c.values())
    for k in ordered:
        if c[k, k].real < 0 or abs(c[k, k].imag) > tolerance:
            raise ValueError("covariance diagonal must be real non-negative PSD")
        for l in ordered:
            if abs(c[k, l] - c[l, k].conjugate()) > tolerance:
                raise ValueError("covariance must be Hermitian")
    n = len(ordered)
    lower = [[0j] * n for _ in range(n)]
    for j in range(n):
        diagonal = c[ordered[j], ordered[j]].real - sum(abs(lower[j][k]) ** 2 for k in range(j))
        if diagonal < -tolerance:
            raise ValueError("covariance must be positive semidefinite")
        # Retain tiny positive independent variances; global tolerance must not
        # erase a weak source merely because another diagonal is much larger.
        lower[j][j] = complex(math.sqrt(diagonal), 0) if diagonal > 0 else 0j
        for i in range(j + 1, n):
            residual = c[ordered[i], ordered[j]] - sum(lower[i][k] * lower[j][k].conjugate() for k in range(j))
            if lower[j][j]:
                lower[i][j] = residual / lower[j][j]
            elif abs(residual) > tolerance:
                raise ValueError("covariance has an inconsistent zero-variance mode")
    modes = []
    for mode in range(n):
        value = sum((h[ordered[i]] * lower[i][mode] for i in range(n)), 0j)
        modes.append({"latent_mode": mode, "response_real_V_rtHz": value.real,
                      "response_imag_V_rtHz": value.imag, "psd_V2_Hz": abs(value) ** 2})
    terms = []
    for k in ordered:
        for l in ordered:
            term = h[k] * c[k, l] * h[l].conjugate()
            terms.append({"band_k": k, "band_l": l, "real_V2_Hz": term.real, "imag_V2_Hz": term.imag})
    out = _result(math.fsum(row["psd_V2_Hz"] for row in modes), source_id=source_id,
                  carrier_peak=carrier_peak, transfer_quantity=transfer_quantity,
                  pm_contract=pm_contract, method="cyclic_covariance_v1")
    out.update(independent_modes=modes, covariance_terms=terms, bands=ordered,
               independent_combination="latent_mode_power_sum", source_psd_units=source_psd_units,
               transfer_units=transfer_units)
    return out


def combine_independent_sources(results, *, carrier_peak=None):
    """Power-sum explicitly independent sources; never merge correlated ports."""
    results = list(results)
    if not results:
        raise ValueError("at least one independent source is required")
    ids, rows = set(), []
    quantity = results[0].get("noise_quantity")
    for result in results:
        if result.get("noise_quantity") != quantity or result.get("psd_convention") != "one_sided":
            raise ValueError("sources must share noise quantity and PSD convention")
        source_id = result.get("source_id")
        if not isinstance(source_id, str) or not source_id or source_id in ids:
            raise ValueError("independent source identifiers must be unique")
        ids.add(source_id)
        _contract(quantity, result.get("pm_projection_contract"))
        rows.append({"source_id": source_id, "psd_V2_Hz": _real(result.get("total_psd_V2_Hz"), "source output PSD", nonnegative=True)})
    out = _result(math.fsum(row["psd_V2_Hz"] for row in rows), source_id="independent_sources",
                  carrier_peak=carrier_peak, transfer_quantity=quantity,
                  pm_contract=results[0].get("pm_projection_contract"), method="independent_source_power_sum_v1")
    out["source_contributions"] = rows
    return out
