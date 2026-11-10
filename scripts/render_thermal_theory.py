#!/usr/bin/env python3
"""Two deterministic, black-and-white THEORY figures; no simulation input.

Only the two named SVG assets are generated. No PDK, PSF, runtime config or
ISF computation is read/changed. --check compares reproducible SVG bytes.
"""

from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile

REPOSITORY = Path(__file__).resolve().parents[1]
PARAMETERS = {"white_coefficient": 1e-4, "device_corner_Hz": 1e5,
              "dc_to_rms_power_ratio": 0.01, "illustrative_floor": 1e-16,
              "other_white_weights": [0.6, 0.3, 0.1]}


def theory_values(frequency):
    """Dimensionless SSB ratios per Hz, deliberately illustrative constants."""
    if not math.isfinite(frequency) or frequency <= 0:
        raise ValueError("positive finite offset frequency required")
    coefficient = PARAMETERS["white_coefficient"]
    eta = PARAMETERS["dc_to_rms_power_ratio"]
    white = coefficient / frequency ** 2
    c0_white = eta * white
    flicker = c0_white * PARAMETERS["device_corner_Hz"] / frequency
    other_white = (1 - eta) * white
    weights = PARAMETERS["other_white_weights"]
    if (len(weights) != 3 or any(not math.isfinite(weight) or weight < 0 for weight in weights)
            or not math.isclose(sum(weights), 1.0, rel_tol=0.0, abs_tol=1e-12)):
        raise ValueError("three nonnegative white weights summing to one required")
    return {"flicker": flicker, "c0_white": c0_white, "other_white": other_white,
            "c1_white": weights[0] * other_white, "c2_white": weights[1] * other_white,
            "c3_white": weights[2] * other_white,
            "c0_channel": flicker + c0_white, "white_total": white,
            "oscillator_total": flicker + white, "floor": PARAMETERS["illustrative_floor"],
            "total_with_floor": flicker + white + PARAMETERS["illustrative_floor"]}


def verify_math():
    fc_device = PARAMETERS["device_corner_Hz"]
    fc_oscillator = fc_device * PARAMETERS["dc_to_rms_power_ratio"]
    fc_floor = math.sqrt(PARAMETERS["white_coefficient"] / PARAMETERS["illustrative_floor"])
    tests = {
        "device_corner_is_c0_flicker_equal_c0_white": math.isclose(theory_values(fc_device)["flicker"], theory_values(fc_device)["c0_white"]),
        "oscillator_corner_is_flicker_equal_all_white": math.isclose(theory_values(fc_oscillator)["flicker"], theory_values(fc_oscillator)["white_total"]),
        "floor_corner_is_all_white_equal_floor": math.isclose(theory_values(fc_floor)["white_total"], PARAMETERS["illustrative_floor"]),
    }
    slopes = {}
    for name, expected in (("flicker", -30), ("white_total", -20), ("c1_white", -20),
                           ("c2_white", -20), ("c3_white", -20), ("floor", 0)):
        slope = 10 * math.log10(theory_values(1000)[name] / theory_values(100)[name])
        slopes[name] = slope
        tests[name + "_slope"] = math.isclose(slope, expected, abs_tol=1e-12)
    for frequency in (10, 100, 1000, 1e4, 1e5, 1e6, 1e8):
        row = theory_values(frequency)
        individual_white = sum(row[name] for name in ("c1_white", "c2_white", "c3_white"))
        tests["individual_white_sum_%g_Hz" % frequency] = math.isclose(individual_white, row["other_white"])
        tests["power_sum_%g_Hz" % frequency] = math.isclose(row["c0_channel"] + individual_white, row["oscillator_total"])
        tests["positive_total_%g_Hz" % frequency] = row["total_with_floor"] >= max(row["flicker"], row["white_total"], row["floor"])
    if not all(tests.values()):
        raise ValueError("theory arithmetic failed: " + repr(tests))
    return {"checks": tests, "asymptotic_slopes_dB_per_decade": slopes,
            "device_corner_Hz": fc_device, "oscillator_corner_Hz": fc_oscillator,
            "white_to_floor_crossing_Hz": fc_floor}


def make_figures(plt):
    frequencies = [10 ** (1 + 7 * i / 700) for i in range(701)]
    values = [theory_values(frequency) for frequency in frequencies]
    def db(name):
        return [10 * math.log10(row[name]) for row in values]
    def setup(title):
        fig, ax = plt.subplots(figsize=(9.4, 6.4))
        fig.subplots_adjust(left=0.12, right=0.975, top=0.88, bottom=0.16)
        ax.set_xscale("log")
        ax.set_xlim(1e1, 1e8)
        ax.set_ylim(-205, -35)
        ax.set_xlabel("Offset frequency f (Hz)", labelpad=8)
        ax.set_ylabel("Illustrative SSB phase noise (dBc/Hz)", labelpad=8)
        ax.grid(which="major", color="0.82", linestyle="-", linewidth=0.6)
        ax.tick_params(which="both", direction="in", top=True, right=True)
        ax.set_title(title, pad=17, fontsize=14, fontweight="bold")
        fig.text(0.5, 0.035, "THEORY ONLY - illustrative constants; no simulation or measurement data",
                 ha="center", fontsize=10, fontweight="bold")
        return fig, ax

    fig_a, ax_a = setup("(a) Device corner versus oscillator corner")
    ax_a.plot(frequencies, db("oscillator_total"), color="black", linewidth=2.8, label="Total = c0 + c1 + c2 + c3 (linear powers)")
    ax_a.plot(frequencies, db("c0_channel"), color="black", linewidth=1.6, linestyle="--", label="c0 channel: low-frequency flicker + its white part")
    for index, style in ((1, ":"), (2, "-."), (3, (0, (5, 2, 1, 2, 1, 2)))):
        weight = PARAMETERS["other_white_weights"][index - 1]
        ax_a.plot(frequencies, db("c%d_white" % index), color="black", linewidth=1.4,
                  linestyle=style, label="c%d white power: %g%% of non-DC white" % (index, weight * 100))
    fc_device = PARAMETERS["device_corner_Hz"]
    fc_oscillator = fc_device * PARAMETERS["dc_to_rms_power_ratio"]
    for corner, label, y in ((fc_oscillator, r"$f_{1/f^3}=10^3$ Hz", -145),
                             (fc_device, r"$f_{1/f}=10^5$ Hz", -190)):
        ax_a.axvline(corner, color="0.45", linestyle="-.", linewidth=0.9)
        ax_a.text(corner * 1.25, y, label, fontsize=11,
                  bbox={"facecolor": "white", "edgecolor": "none", "pad": 2})
    for corner, component in ((fc_oscillator, "oscillator_total"), (fc_device, "c0_channel")):
        ax_a.plot([corner], [10 * math.log10(theory_values(corner)[component])],
                  marker="o", markersize=6, markerfacecolor="white", markeredgecolor="black", zorder=5)
    ax_a.text(0.04, 0.17, r"Chosen case: $|c_0|^2/\Gamma_{rms}^2=0.01$" + "\nNot a universal strict ordering for all VCOs",
              transform=ax_a.transAxes, fontsize=10,
              bbox={"facecolor": "white", "edgecolor": "0.5", "pad": 5})
    ax_a.legend(loc="upper right", frameon=True, facecolor="white", edgecolor="black", fontsize=9)

    fig_b, ax_b = setup("(b) Typical asymptotes and their linear-power total")
    ax_b.plot(frequencies, db("total_with_floor"), color="black", linewidth=2.8, label="Total: flicker + white + illustrative floor")
    ax_b.plot(frequencies, db("flicker"), color="black", linewidth=1.4, linestyle="--", label="Flicker-upconverted component: -30 dB/dec")
    ax_b.plot(frequencies, db("white_total"), color="black", linewidth=1.4, linestyle=":", label="White-noise conversion: -20 dB/dec")
    ax_b.plot(frequencies, db("floor"), color="black", linewidth=1.2, linestyle="-.", label="Illustrative far-offset floor: 0 dB/dec")
    ax_b.text(60, -68, "-30 dB/dec", rotation=-43, fontsize=10, bbox={"facecolor": "white", "edgecolor": "none", "pad": 1})
    ax_b.text(2e4, -125, "-20 dB/dec", rotation=-33, fontsize=10, bbox={"facecolor": "white", "edgecolor": "none", "pad": 1})
    ax_b.text(4e6, -151, "floor", fontsize=11)
    ax_b.text(0.035, 0.10, "Schematic regions only: real slopes/visible corners may differ.\nThe production calculation does not add a fitted floor.",
              transform=ax_b.transAxes, fontsize=10,
              bbox={"facecolor": "white", "edgecolor": "0.5", "pad": 5})
    ax_b.legend(loc="upper right", frameon=True, facecolor="white", edgecolor="black", fontsize=9)
    return {"thermal_theory_corners.svg": fig_a, "thermal_theory_slopes.svg": fig_b}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=REPOSITORY / "docs" / "assets")
    parser.add_argument("--check", action="store_true", help="compare existing SVG bytes; do not overwrite")
    args = parser.parse_args(argv)
    mathematical_checks = verify_math()
    # Matplotlib's font cache remains private and temporary; no desktop opens.
    with tempfile.TemporaryDirectory(prefix="isf-theory-mpl-") as config_dir:
        os.environ["MPLCONFIGDIR"] = config_dir
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import pyplot as plt
        matplotlib.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
            "svg.hashsalt": "isf-thermal-theory-v1", "svg.fonttype": "path",
            "axes.edgecolor": "black", "axes.labelcolor": "black", "text.color": "black",
            "figure.facecolor": "white", "axes.facecolor": "white"})
        if not args.check:
            args.output_dir.mkdir(parents=True, exist_ok=True)
        records = []
        for filename, figure in make_figures(plt).items():
            content = io.BytesIO()
            figure.savefig(content, format="svg", metadata={"Date": None,
                "Creator": "VCO ISF Explorer deterministic thermal theory v1",
                "Description": "THEORY ONLY. No measurements, PSF, PDK or fitted values. " + json.dumps(PARAMETERS, sort_keys=True)})
            plt.close(figure)
            # Matplotlib emits trailing blanks inside multiline SVG paths.
            # Normalize formatting only; keep every coordinate/label intact.
            raw = b"\n".join(line.rstrip() for line in content.getvalue().splitlines()) + b"\n"
            target = args.output_dir / filename
            matches = target.is_file() and target.read_bytes() == raw
            if not args.check:
                target.write_bytes(raw)
                target.chmod(0o644)
                matches = True
            records.append({"path": str(target.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "pass": matches})
        report = {"schema_version": 1, "theory_only": True, "parameters": PARAMETERS,
                  "matplotlib_version": matplotlib.__version__, "math": mathematical_checks,
                  "figures": records, "check_only": args.check, "ok": all(item["pass"] for item in records)}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
