"""Pure-Python harmonic closure math used by both UI import modes."""

from __future__ import annotations

import cmath
import math
from typing import Iterable, Mapping


ComplexMap = Mapping[int, complex]


def wrap_phase(value: float) -> float:
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def phasor(magnitude: float, phase_deg: float) -> complex:
    return float(magnitude) * cmath.exp(1j * math.radians(float(phase_deg)))


def signed_real_coefficients(positive: ComplexMap) -> dict[int, complex]:
    """Expand coefficients of a real waveform using conjugate symmetry."""
    if 0 not in positive:
        raise ValueError("k=0 coefficient is required")
    zero = complex(positive[0])
    zero_tolerance = 1.0e-15 * max(abs(zero.real), 1.0e-300)
    if abs(zero.imag) > zero_tolerance:
        raise ValueError("k=0 coefficient of a real waveform must be real")
    out = {0: complex(zero.real, 0.0)}
    for k, value in positive.items():
        if k < 0:
            raise ValueError("input must contain non-negative harmonics only")
        if k:
            out[int(k)] = complex(value)
            out[-int(k)] = complex(value).conjugate()
    return out


def coherent_sum(transfer: ComplexMap, source: ComplexMap) -> complex:
    common = sorted(set(transfer) & set(source))
    return sum((complex(transfer[k]) * complex(source[k]) for k in common), 0j)


def harmonic_contributions(transfer: ComplexMap, positive: ComplexMap) -> list[dict]:
    rows = []
    cumulative = 0j
    for k in sorted(positive):
        if k == 0:
            value = complex(transfer.get(0, 0j)) * complex(positive[0])
        else:
            value = (
                complex(transfer.get(k, 0j)) * complex(positive[k])
                + complex(transfer.get(-k, 0j)) * complex(positive[k]).conjugate()
            )
        cumulative += value
        rows.append(
            {
                "k": k,
                "Q_real": value.real,
                "Q_imag": value.imag,
                "Q_mag": abs(value),
                "Q_phase_deg": math.degrees(cmath.phase(value)) if value else 0.0,
                "cum_real": cumulative.real,
                "cum_imag": cumulative.imag,
                "cum_mag": abs(cumulative),
            }
        )
    return rows


def reconstruct(coefficients: ComplexMap, samples: int = 257) -> list[dict]:
    if samples < 2:
        raise ValueError("samples must be >=2")
    max_k = max(coefficients, default=0)
    rows = []
    for index in range(samples):
        wt = 2.0 * math.pi * index / (samples - 1)
        value = complex(coefficients.get(0, 0j)).real
        for k in range(1, max_k + 1):
            value += 2.0 * (complex(coefficients.get(k, 0j)) * cmath.exp(1j * k * wt)).real
        rows.append({"wt_rad": wt, "value": value})
    return rows


def fourier_coefficients(wt: Iterable[float], values: Iterable[float], max_k: int) -> dict[int, complex]:
    """Trapezoidal Fourier integral for x=M0+2Re sum(Mk exp(jkwt))."""
    pairs = sorted((float(x), float(y)) for x, y in zip(wt, values))
    if len(pairs) < 2:
        raise ValueError("at least two waveform samples are required")
    if pairs[0][0] > 1e-12:
        pairs.insert(0, (0.0, pairs[-1][1]))
    if pairs[-1][0] < 2.0 * math.pi - 1e-12:
        pairs.append((2.0 * math.pi, pairs[0][1]))
    else:
        pairs[-1] = (pairs[-1][0], pairs[0][1])
    result: dict[int, complex] = {}
    for k in range(max_k + 1):
        integral = 0j
        for (x0, y0), (x1, y1) in zip(pairs, pairs[1:]):
            f0 = y0 * cmath.exp(-1j * k * x0)
            f1 = y1 * cmath.exp(-1j * k * x1)
            integral += 0.5 * (f0 + f1) * (x1 - x0)
        result[k] = integral / (2.0 * math.pi)
    return result


def fit_uniform_delay_phase(mdc: ComplexMap, mdyn: ComplexMap) -> float:
    """Return phase slope phi where Mdelay[k]=Mdc[k]*exp(-j*k*phi)."""
    terms = []
    previous = None
    shift = 0.0
    for k in sorted(set(mdc) & set(mdyn)):
        if k <= 0 or not mdc[k] or not mdyn[k]:
            continue
        phase = wrap_phase(cmath.phase(mdyn[k]) - cmath.phase(mdc[k]))
        if previous is not None:
            while phase + shift - previous > math.pi:
                shift -= 2.0 * math.pi
            while phase + shift - previous < -math.pi:
                shift += 2.0 * math.pi
        unwrapped = phase + shift
        previous = unwrapped
        terms.append((float(k), unwrapped, abs(mdc[k]) ** 2))
    denominator = sum(weight * k * k for k, _, weight in terms)
    if denominator == 0.0:
        return 0.0
    slope = sum(weight * k * phase for k, phase, weight in terms) / denominator
    return -slope


def apply_uniform_delay(mdc: ComplexMap, phase: float) -> dict[int, complex]:
    return {k: complex(value) * cmath.exp(-1j * k * phase) for k, value in mdc.items()}


def exact_gamma(
    transfer: ComplexMap,
    rotation: complex,
    carrier_peak: float,
    offset_hz: float,
    max_k: int,
) -> dict[int, complex]:
    if carrier_peak <= 0.0:
        raise ValueError("carrier_peak must be positive")
    scale = math.sqrt(2.0) * 2.0 * math.pi * float(offset_hz) / float(carrier_peak)
    gamma = {0: complex(scale * (rotation * transfer[0]).real, 0.0)}
    for k in range(1, max_k + 1):
        gamma[k] = 0.5 * scale * (
            (rotation * transfer[k]).conjugate() + rotation * transfer[-k]
        )
    return gamma


def carrier_referenced_gamma(
    transfer: ComplexMap,
    carrier: complex,
    offset_hz: float,
    max_k: int,
) -> dict[int, complex]:
    """Build signed-H ISF coefficients from the carrier reference only.

    The rotation must not be inferred from ``Mdyn`` or its output.  Doing so
    makes the displayed ISF noise-dependent and turns the equivalence check
    into a circular identity.  Both signed HBXF sidebands are retained here;
    the older positive-sideband-only 4*pi expression is not equivalent in
    general and is lower by 3.01 dB for the bundled synthetic example.
    """

    carrier = complex(carrier)
    if not carrier:
        raise ValueError("carrier phasor must be non-zero")
    rotation = carrier.conjugate() / abs(carrier)
    return exact_gamma(transfer, rotation, abs(carrier), offset_hz, max_k)


def carrier_referenced_gamma_branches(
    transfer: ComplexMap,
    carrier: complex,
    offset_hz: float,
    max_k: int,
) -> dict[str, dict[int, complex]]:
    """Return independent carrier-I and carrier-Q signed-H branches.

    A single real periodic ISF is a linear projection and therefore cannot be
    equal to ``abs(sum(H[k] * M[k]))`` for arbitrary complex source
    coefficients.  Two fixed, noise-independent branches are sufficient:
    their signed sums are the carrier-referenced real and imaginary output
    components, and their RSS is exactly the physical output magnitude.
    """

    carrier = complex(carrier)
    if not carrier:
        raise ValueError("carrier phasor must be non-zero")
    rotation_i = carrier.conjugate() / abs(carrier)
    rotation_q = -1j * rotation_i
    return {
        "i": exact_gamma(
            transfer, rotation_i, abs(carrier), offset_hz, max_k
        ),
        "q": exact_gamma(
            transfer, rotation_q, abs(carrier), offset_hz, max_k
        ),
    }


def gamma_sum(gamma: ComplexMap, positive: ComplexMap) -> complex:
    value = complex(gamma.get(0, 0j)) * complex(positive.get(0, 0j))
    for k in sorted(set(gamma) & set(positive)):
        if k > 0:
            value += 2.0 * (complex(gamma[k]) * complex(positive[k]).conjugate()).real
    return complex(value)


def _positive_finite_status(*values: float) -> str:
    if all(math.isfinite(float(value)) and float(value) > 0.0 for value in values):
        return "available"
    return "not_applicable"


def dBc_from_pm_asd(pm_asd: float, carrier_peak: float) -> float | None:
    """Return carrier-referenced noise, or None when the ratio is undefined."""

    if _positive_finite_status(pm_asd, carrier_peak) != "available":
        return None
    return 10.0 * math.log10(2.0) + 20.0 * (
        math.log10(float(pm_asd)) - math.log10(float(carrier_peak))
    )


def error_dB(predicted_asd: float, reference_asd: float) -> float | None:
    """Return a finite ratio error, or None for a zero/invalid ratio."""

    if _positive_finite_status(predicted_asd, reference_asd) != "available":
        return None
    return 20.0 * (
        math.log10(float(predicted_asd)) - math.log10(float(reference_asd))
    )


def combine_independent_device_noise(
    outputs: Iterable[complex | float], carrier_peak: float | None = None
) -> dict:
    """Combine independent device output-noise sources strictly in power.

    Each item is either a complex output-noise phasor in V/√Hz or its positive
    ASD.  Relative phasor angles are intentionally ignored because different
    transistor noise sources are independent.
    """
    values = [complex(value) for value in outputs]
    if not values:
        raise ValueError("at least one independent device output is required")
    powers = [abs(value) ** 2 for value in values]
    total_psd = sum(powers)
    total_asd = math.sqrt(total_psd)
    dbc_status = (
        "not_requested"
        if carrier_peak is None
        else _positive_finite_status(total_asd, carrier_peak)
    )
    return {
        "device_asd_V_rtHz": [abs(value) for value in values],
        "device_psd_V2_Hz": powers,
        "total_pm_psd_V2_Hz": total_psd,
        "total_pm_asd_V_rtHz": total_asd,
        "dBc_Hz": (
            dBc_from_pm_asd(total_asd, carrier_peak)
            if carrier_peak is not None else None
        ),
        "dBc_Hz_status": dbc_status,
        "combination": "independent_power_sum",
    }


def calculate_point(
    transfer: ComplexMap,
    mdc_positive: ComplexMap,
    mdyn_positive: ComplexMap,
    carrier_peak: float,
    offset_hz: float,
    reference_pm_asd: float | None = None,
    f0_hz: float | None = None,
    samples: int = 257,
    carrier_phasor: complex | None = None,
) -> dict:
    """Calculate physical closure and an independent signed-H ISF check."""
    max_k = max(mdyn_positive)
    required = set(range(-max_k, max_k + 1))
    missing = sorted(required - set(transfer))
    if missing:
        raise ValueError(f"missing signed transfer harmonics: {missing}")
    mdc_signed = signed_real_coefficients(mdc_positive)
    mdyn_signed = signed_real_coefficients(mdyn_positive)
    delay_phase = fit_uniform_delay_phase(mdc_positive, mdyn_positive)
    mdelay_positive = apply_uniform_delay(mdc_positive, delay_phase)
    mdelay_signed = signed_real_coefficients(mdelay_positive)
    voltages = {
        "dc": coherent_sum(transfer, mdc_signed),
        "delay": coherent_sum(transfer, mdelay_signed),
        "dynamic": coherent_sum(transfer, mdyn_signed),
    }
    carrier_reference = (
        complex(carrier_phasor)
        if carrier_phasor is not None
        else complex(carrier_peak, 0.0)
    )
    if not carrier_reference:
        raise ValueError("carrier phasor must be non-zero")
    if not math.isclose(
        abs(carrier_reference),
        float(carrier_peak),
        rel_tol=1.0e-12,
        abs_tol=1.0e-18,
    ):
        raise ValueError("carrier_peak and carrier_phasor magnitude disagree")
    rotation = carrier_reference.conjugate() / abs(carrier_reference)
    gamma_branches = carrier_referenced_gamma_branches(
        transfer, carrier_reference, offset_hz, max_k
    )
    gamma_i = gamma_branches["i"]
    gamma_q = gamma_branches["q"]
    heff_i = gamma_sum(gamma_i, mdyn_positive).real
    heff_q = gamma_sum(gamma_q, mdyn_positive).real
    heff_rss = math.hypot(heff_i, heff_q)
    scale = math.sqrt(2.0) * 2.0 * math.pi * offset_hz / carrier_peak
    rotated_dynamic = rotation * voltages["dynamic"]
    carrier_i_projection = rotated_dynamic.real
    carrier_q_projection = rotated_dynamic.imag
    projected_i_heff = scale * carrier_i_projection
    projected_q_heff = scale * carrier_q_projection
    magnitude_heff = scale * abs(voltages["dynamic"])
    gamma_i_rows = reconstruct(gamma_i, samples)
    gamma_q_rows = reconstruct(gamma_q, samples)
    mdc_rows = reconstruct(mdc_positive, samples)
    mdyn_rows = reconstruct(mdyn_positive, samples)
    product_i = [
        {"wt_rad": g["wt_rad"], "value": g["value"] * m["value"]}
        for g, m in zip(gamma_i_rows, mdyn_rows)
    ]
    product_q = [
        {"wt_rad": g["wt_rad"], "value": g["value"] * m["value"]}
        for g, m in zip(gamma_q_rows, mdyn_rows)
    ]
    methods = {}
    reference_requested = reference_pm_asd is not None
    for name, voltage in voltages.items():
        asd = abs(voltage)
        methods[name] = {
            "pm_asd_V_rtHz": asd,
            "dBc_Hz": dBc_from_pm_asd(asd, carrier_peak),
            "dBc_Hz_status": _positive_finite_status(asd, carrier_peak),
            "error_dB": (
                error_dB(asd, reference_pm_asd)
                if reference_requested else None
            ),
            "error_dB_status": (
                _positive_finite_status(asd, reference_pm_asd)
                if reference_requested else "not_requested"
            ),
        }
    return {
        "methods": methods,
        "uniform_delay_phase_rad": delay_phase,
        "uniform_delay_s": (
            delay_phase / (2.0 * math.pi * f0_hz)
            if f0_hz is not None and f0_hz > 0.0
            else None
        ),
        "physical_vdyn": {"real": voltages["dynamic"].real, "imag": voltages["dynamic"].imag},
        "heff_dynamic_exact": heff_rss,
        "heff_dynamic_carrier_referenced": heff_rss,
        "heff_carrier_i_signed": heff_i,
        "heff_carrier_q_signed": heff_q,
        "heff_branch_rss": heff_rss,
        "heff_from_carrier_projection": abs(projected_i_heff),
        "heff_from_carrier_i_projection": projected_i_heff,
        "heff_from_carrier_q_projection": projected_q_heff,
        "heff_from_physical_units": magnitude_heff,
        "gamma_i_algebra_error_dB": error_dB(
            abs(heff_i), abs(projected_i_heff)
        ),
        "gamma_q_algebra_error_dB": error_dB(
            abs(heff_q), abs(projected_q_heff)
        ),
        "gamma_equivalence_error_dB": error_dB(
            heff_rss, magnitude_heff
        ),
        "gamma_to_output_magnitude_error_dB": error_dB(
            heff_rss, magnitude_heff
        ),
        "carrier_projection_V_rtHz": abs(carrier_i_projection),
        "carrier_i_projection_V_rtHz": carrier_i_projection,
        "carrier_q_projection_V_rtHz": carrier_q_projection,
        "gamma_reference": "carrier_referenced_signed_H_iq_v1",
        "harmonic_contributions": harmonic_contributions(transfer, mdyn_positive),
        "curves": {
            "gamma": gamma_i_rows,
            "gamma_i": gamma_i_rows,
            "gamma_q": gamma_q_rows,
            "m_dc": mdc_rows,
            "m_dyn": mdyn_rows,
            "product": product_i,
            "product_i": product_i,
            "product_q": product_q,
        },
    }
