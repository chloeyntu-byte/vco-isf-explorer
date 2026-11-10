#!/usr/bin/env python3
"""Read-only verification of the bundled, PDK-free thermal references.

Never invokes Spectre/Ocean or writes/export PSF files. The optional trusted
Cadence environment is sourced only inside each PSF-reader child process.
Exit codes: 0 checks pass, 1 numerical/provenance check fails, 2 input/tool error.
"""

from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from isf_tool.core.noise_sources import noise_quantity, noise_sample, parse_psf_ascii
from isf_tool.thermal import project_am_pm

SCHEMA_VERSION = 1
REFERENCE_ID = "spectre18_analytic_white_AM_PM_PWM_20260920_v1"
INDUCTOR_REFERENCE_ID = "spectre18_known_R_and_skin_R_voltage_noise_20260920_v1"
F0_HZ, OFFSET_HZ, WHITE_PSD_V2_HZ = 1e6, 1e3, 1e-18
EXPECTED_PSD = {"am": {"am": 3.75e-19, "pm": 1.25e-19},
                "pm": {"am": 1.25e-19, "pm": 3.75e-19},
                "pwm": {"am": 1.40625e-19, "pm": 3.90625e-19}}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def close(actual, expected, relative=1e-9, absolute=1e-35):
    return math.isfinite(actual) and math.isclose(actual, expected, rel_tol=relative, abs_tol=absolute)


def comparison(actual, expected, relative=1e-9):
    if expected <= 0 or actual < 0:
        raise ValueError("comparison requires positive reference and nonnegative result")
    return {"actual": actual, "expected": expected,
            "relative_error": (actual - expected) / expected,
            "error_dB": 10 * math.log10(actual / expected) if actual > 0 else None,
            "relative_tolerance": relative, "pass": close(actual, expected, relative)}


class Reader:
    def __init__(self, psf_tool, cadence_env=None):
        self.tool = str(psf_tool)
        self.environment = str(Path(cadence_env).expanduser().resolve(strict=True)) if cadence_env else None
        self.cache = {}
        self.manifest = {}
        self.versions = set()

    def command(self, args):
        if self.environment:
            # Positional arguments avoid shell interpolation of paths. Cadence
            # library variables cannot leak back into the parent Python process.
            return ["bash", "-c", 'source "$1" >/dev/null || exit; shift; exec "$@"',
                    "thermal-reference-reader", self.environment, *args]
        return args

    def record(self, path):
        path = Path(path).resolve(strict=True)
        if not path.is_file():
            raise ValueError("not a regular evidence file: " + str(path))
        self.manifest[str(path)] = {"sha256": sha256(path), "size_bytes": path.stat().st_size}
        return path

    def read(self, path):
        path = self.record(path)
        if path in self.cache:
            return self.cache[path]
        completed = subprocess.run(self.command([self.tool, "-f", "%.16e", "-i", str(path)]),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, timeout=60, check=False)
        if completed.returncode:
            raise ValueError("PSF reader failed (%s): %s\n%s" %
                             (completed.returncode, path, completed.stderr.strip()))
        dataset = parse_psf_ascii(completed.stdout)
        if dataset["header"].get("simulator") != "spectre":
            raise ValueError("evidence is not a Spectre dataset: " + str(path))
        version = str(dataset["header"].get("version", ""))
        if not version:
            raise ValueError("missing simulator version: " + str(path))
        self.versions.add(version)
        self.cache[path] = dataset
        return dataset

    def complex_value(self, path, frequency, trace, units):
        dataset = self.read(path)
        name = dataset["traces"].get(trace)
        if name is None or dataset["types"][name].get("units") != units:
            raise ValueError("missing/wrong transfer units for %s in %s" % (trace, path))
        value = noise_sample(dataset, frequency)["values"][trace]
        if not isinstance(value, complex):
            raise ValueError("complex response required: " + str(path))
        return value


def provenance(reader, psf, fixture_name, filenames):
    output = []
    for filename in filenames:
        actual = reader.record(psf.parent / "netlist" / filename)
        # The Verilog-A source is stored with a .txt suffix in the repository;
        # the Spectre netlist directory holds it under its real name.
        stored = filename + ".txt" if filename.endswith(".va") else filename
        fixture = reader.record(REPOSITORY / "tests" / "fixtures" / "thermal_reference" / fixture_name / stored)
        output.append({"file": filename, "pass": sha256(actual) == sha256(fixture),
                       "actual_sha256": sha256(actual), "fixture_sha256": sha256(fixture)})
    log = reader.record(psf / "spectre.out")
    completions = re.findall(r"spectre completes with (\d+) errors, (\d+) warnings", log.read_text(errors="replace"))
    if not completions:
        raise ValueError("no terminal Spectre completion evidence: " + str(log))
    errors, warnings = map(int, completions[-1])
    output.append({"file": "spectre.out", "pass": errors == 0, "errors": errors, "warnings": warnings})
    return output


def verify_am_pm(reader, psf):
    psf = Path(psf).expanduser().resolve(strict=True)
    checks = provenance(reader, psf, "analytic_am_pm", ["input.scs", "white_source.va"])
    carrier = reader.complex_value(psf / "hb.fd.pss_hb", F0_HZ, "carrier", "V")
    checks.append({"check": "unit peak sine carrier", "pass": abs(carrier + 1j) < 1e-9,
                   "real_V": carrier.real, "imag_V": carrier.imag})
    cases = []
    # HB has 8 harmonics. This common domain is explicitly measured on both
    # signed outputs; the fixture's nonzero paths are inside |m| <= 3.
    bands = range(-7, 8)
    for case in EXPECTED_PSD:
        upper, negative_lower = {}, {}
        for m in bands:
            upper[m] = reader.complex_value(psf / ("%s_upper.%d.pxf_hbxf" % (case, m - 1)),
                                            F0_HZ + OFFSET_HZ, "VSTIM", "V/V")
            negative_lower[m] = reader.complex_value(psf / ("%s_lower.%d.pxf_hbxf" % (case, -m - 1)),
                                                     F0_HZ - OFFSET_HZ, "VSTIM", "V/V").conjugate()
        # Do not self-certify with validated=True: this script is the independent
        # experiment which checks the projection before production accepts it.
        projection = project_am_pm(upper, negative_lower, carrier)
        for mode in ("am", "pm"):
            response = projection[mode + "_transfer"]
            parts = [{"input_band": m, "source_frequency_Hz": abs(m * F0_HZ + OFFSET_HZ),
                      "transfer_real": response[m].real, "transfer_imag": response[m].imag,
                      "psd_V2_Hz": abs(response[m]) ** 2 * WHITE_PSD_V2_HZ} for m in bands]
            prediction = math.fsum(part["psd_V2_Hz"] for part in parts)
            dataset = reader.read(psf / ("noise_%s.%s.pnoise_hbnoise" % (case, mode)))
            measured = noise_quantity(dataset, OFFSET_HZ, "out", expected_domain="V")
            contributor = noise_quantity(dataset, OFFSET_HZ, "W", "known_white", expected_domain="V")
            if measured["raw_quantity"] != "ASD" or contributor["raw_quantity"] != "PSD":
                raise ValueError("unexpected HBNoise output/contributor quantity")
            rows = {"transfer_vs_hbnoise": comparison(prediction, measured["psd"]),
                    "transfer_vs_analytic": comparison(prediction, EXPECTED_PSD[case][mode]),
                    "hbnoise_vs_analytic": comparison(measured["psd"], EXPECTED_PSD[case][mode]),
                    "contributor_vs_output": comparison(contributor["psd"], measured["psd"])}
            cases.append({"case": case, "projection": mode, "predicted_psd_V2_Hz": prediction,
                          "hbnoise_psd_V2_Hz": measured["psd"], "checks": rows,
                          "pass": all(item["pass"] for item in rows.values()),
                          "input_band_contributions": parts})
        if case == "am":
            checks.append({"check": "pure baseband AM has zero PM at m=0",
                           "pass": abs(projection["pm_transfer"][0]) < 1e-9})
    return {"reference_id": REFERENCE_ID, "psf": str(psf), "f0_Hz": F0_HZ,
            "offset_Hz": OFFSET_HZ, "source_psd_V2_Hz": WHITE_PSD_V2_HZ,
            "source_is_broadband": True, "checks": checks, "cases": cases,
            "pass": all(item["pass"] for item in checks + cases)}


def verify_inductor(reader, psf):
    psf = Path(psf).expanduser().resolve(strict=True)
    checks = provenance(reader, psf, "inductor_units", ["input.scs"])
    samples = []
    skin_density = []
    for device, analysis, node in (("Lfixed", "nfixed", "out_fixed"), ("Lskin", "nskin", "out_skin")):
        dataset = reader.read(psf / (analysis + ".noise"))
        for frequency in (1e4, 1e6, 5e9, 1e10):
            source = noise_quantity(dataset, frequency, device, "rn.src")
            measured = noise_quantity(dataset, frequency, device, "rn.out", expected_domain="V")
            fields = noise_sample(dataset, frequency)["values"][device]
            gain = reader.complex_value(psf / "ac.ac", frequency, node, "V") / reader.complex_value(psf / "ac.ac", frequency, "vin", "V")
            if source["raw_quantity"] != "PSD" or source["psd_units"] != "A^2/Hz":
                raise ValueError("inductor source label differs; re-examine dimensional evidence")
            # Restricted to this known inductor fixture: the native A^2/Hz
            # declaration is retained in evidence, not used to infer port type.
            prediction = abs(gain) ** 2 * source["psd"]
            rows = {"independent_ac_voltage_transfer_vs_rn_out": comparison(prediction, measured["psd"], 1e-5),
                    "xf2_vs_independent_ac_gain_squared": comparison(fields["rn.xf2"], abs(gain) ** 2, 1e-5),
                    "source_times_xf2_vs_rn_out": comparison(source["psd"] * fields["rn.xf2"], measured["psd"])}
            if device == "Lfixed":
                # Modern exact SI k_B; Spectre 18.1 uses an older constant.
                rows["source_vs_4kTR_modern_SI"] = comparison(source["psd"], 4 * 1.380649e-23 * 300.15 * 10, 3e-5)
            else:
                skin_density.append(source["psd"])
            samples.append({"device": device, "frequency_Hz": frequency,
                            "raw_source_units": source["psd_units"], "physical_source_units": "V^2/Hz",
                            "source_psd": source["psd"], "independent_transfer_units": "V/V",
                            "predicted_psd_V2_Hz": prediction, "rn_out_psd_V2_Hz": measured["psd"],
                            "checks": rows, "pass": all(item["pass"] for item in rows.values())})
    checks.append({"check": "skin source PSD increases with frequency",
                   "pass": all(a < b for a, b in zip(skin_density, skin_density[1:]))})
    return {"reference_id": INDUCTOR_REFERENCE_ID, "psf": str(psf), "checks": checks,
            "samples": samples, "pass": all(item["pass"] for item in checks + samples),
            "scope": "Only the tested native inductor rn series-voltage source; not a global PSF unit rewrite.",
            "top_level_out_used": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--psf", required=True, help="existing analytic_am_pm/psf directory")
    parser.add_argument("--inductor-psf", help="existing inductor_units/psf directory; optional independent unit check")
    parser.add_argument("--psf-tool", default="psf", help="Cadence psf executable (never spectre)")
    parser.add_argument("--cadence-env", help="trusted environment script, sourced only in PSF reader children")
    args = parser.parse_args(argv)
    report = {"schema_version": SCHEMA_VERSION, "verifier": "verify_thermal_reference_v1",
              "read_only": True, "simulation_started": False, "signoff": False,
              "requested_suites": ["am_pm"] + (["inductor_units"] if args.inductor_psf else [])}
    reader = None
    try:
        reader = Reader(args.psf_tool, args.cadence_env)
        for relative in ("scripts/verify_thermal_reference.py", "isf_tool/thermal.py", "isf_tool/core/noise_sources.py"):
            reader.record(REPOSITORY / relative)
        if reader.environment:
            reader.record(reader.environment)
        if Path(args.psf_tool).is_file():
            reader.record(args.psf_tool)
        report["am_pm"] = verify_am_pm(reader, args.psf)
        if args.inductor_psf:
            report["inductor_units"] = verify_inductor(reader, args.inductor_psf)
        report["ok"] = all(report[key]["pass"] for key in report["requested_suites"])
        report["simulator_versions"] = sorted(reader.versions)
        if len(reader.versions) != 1:
            report["ok"] = False
            report["version_error"] = "reference artifacts mix simulator versions"
        code = 0 if report["ok"] else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        report.update(ok=False, error=type(exc).__name__ + ": " + str(exc))
        code = 2
    report["evidence_files"] = reader.manifest if reader else {}
    report["exit_code"] = code
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
