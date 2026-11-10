#!/usr/bin/env python3
"""Development utility: export a complete canonical CSV fixture from an audited run."""

from __future__ import annotations

import argparse
import csv
import copy
import datetime
import hashlib
import importlib.util
import json
import math
import re
import sys
import time
from pathlib import Path


BOOTSTRAP_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_ROOTS = [BOOTSTRAP_ROOT]
if BOOTSTRAP_ROOT.name == "_runtime" and BOOTSTRAP_ROOT.parent.name == "isf_tool":
    BOOTSTRAP_ROOTS.append(BOOTSTRAP_ROOT.parent.parent)
for bootstrap_root in reversed(BOOTSTRAP_ROOTS):
    if str(bootstrap_root) not in sys.path:
        sys.path.insert(0, str(bootstrap_root))

from isf_tool.core.source_paths import (
    VCO_NETLIST_RELATIVE,
    resolve_source_path,
)
from isf_tool.resources import RUNTIME_ROOT, script_path
from isf_tool.core.result_presentation import SUMMARY_QUALITY_FIELDS, enrich_result, enrich_summary
from isf_tool.core.dual_tb import spectre_noise_evidence
from isf_tool.core.thermal_budget import thermal_evidence


TOOL_ROOT = Path(RUNTIME_ROOT)


def rows(path: Path):
    from isf_tool.core.limits import MAX_CANONICAL_CSV_FIELD_BYTES
    csv.field_size_limit(MAX_CANONICAL_CSV_FIELD_BYTES)
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def noise_evidence_csv_fields(summary):
    """Portable optional CSV fields; old target-device totals remain unavailable."""
    evidence = spectre_noise_evidence(summary)
    return {
        "spectre_total_pm_asd_V_rtHz": evidence["spectre_total_pm_asd_V_rtHz"],
        "spectre_noise_contributions_json": json.dumps(evidence["spectre_noise_contributions"], allow_nan=False, sort_keys=True),
        "spectre_noise_reference_json": json.dumps(evidence["spectre_noise_reference"], allow_nan=False, sort_keys=True),
        "thermal_noise_json": json.dumps(thermal_evidence(summary), allow_nan=False, sort_keys=True),
    }


def write(path: Path, fieldnames, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(values)


def file_sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_identity(root: Path):
    digest = hashlib.sha256()
    count = 0
    size = 0
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        digest.update(b"\0")
        count += 1
    return {
        "sha256": digest.hexdigest(),
        "files": count,
        "bytes": size,
    }


def source_netlist_path(cfg, point):
    return Path(
        resolve_source_path(
            cfg["source_root"],
            point["source_dir"],
            VCO_NETLIST_RELATIVE,
        )
    )


def operating_point_metadata(config, naming):
    """Retain evaluated Maestro settings separately from the noise offset."""
    result = {}
    for point in config.get("points", []):
        name = naming(point)
        if name in result:
            raise RuntimeError("duplicate operating point identity during export: " + name)
        contract = copy.deepcopy(point.get("operating_point") or {})
        contract.setdefault("params", {})
        contract.setdefault("corner", point.get("corner"))
        result[name] = contract
    return result


def value_at(coeffs, wt):
    value = complex(coeffs.get(0, 0j)).real
    for k in range(1, max(coeffs, default=0) + 1):
        value += 2.0 * (complex(coeffs.get(k, 0j)) * __import__("cmath").exp(1j * k * wt)).real
    return value


def interpolate_periodic(workpoint, key, wt):
    source = sorted((float(row["wt_rad"]), float(row[key])) for row in workpoint)
    for left, right in zip(source, source[1:]):
        if left[0] <= wt <= right[0]:
            fraction = (wt - left[0]) / (right[0] - left[0]) if right[0] != left[0] else 0.0
            return left[1] + fraction * (right[1] - left[1])
    return source[-1][1]


def _csv_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "pass")


def classify_export(
    summary_rows,
    job_id=None,
    config=None,
    runtime_numerical_quality=None,
    non_signoff=False,
    formal_signoff=False,
):
    """Fail closed for formal export; ordinary replay remains lightweight.

    A job id is traceability only.  Formal intent must be explicit; ordinary
    and smoke exports remain replay even when they retain a job id.  Formal
    exports require the agreed six-point minimum and clean numerical-quality
    gates.  HBAC remains a secondary diagnostic unless the config explicitly
    promotes it to a signoff requirement.
    """

    config = config or {}
    point_count = len(summary_rows)
    summary_numerical_quality_all_pass = bool(summary_rows) and all(
        _csv_bool(row.get("numerical_quality_pass"))
        for row in summary_rows
    )
    runtime_numerical_quality_pass = bool(
        runtime_numerical_quality is None
        or runtime_numerical_quality.get("pass", False)
    )
    numerical_quality_all_pass = bool(
        summary_numerical_quality_all_pass
        and runtime_numerical_quality_pass
    )
    hbac_secondary_all_pass = bool(summary_rows) and all(
        _csv_bool(row.get("hbac_secondary_validation_pass"))
        for row in summary_rows
    )
    pm_magnitude_projection_all_pass = bool(summary_rows) and all(
        _csv_bool(row.get("pm_magnitude_projection_valid"))
        for row in summary_rows
    )
    status = {
        "run_kind": "replay",
        "signoff": False,
        "point_count": point_count,
        "formal_point_count_qualified": point_count >= 6,
        "numerical_quality_all_pass": numerical_quality_all_pass,
        "summary_numerical_quality_all_pass": (
            summary_numerical_quality_all_pass
        ),
        "runtime_numerical_quality_pass": runtime_numerical_quality_pass,
        "runtime_numerical_quality_issues": list(
            (runtime_numerical_quality or {}).get("issues", [])
        ),
        "hbac_secondary_all_pass": hbac_secondary_all_pass,
        "pm_magnitude_projection_all_pass": pm_magnitude_projection_all_pass,
        "hbac_required_for_signoff": bool(
            config.get("require_hbac_validation_for_signoff", False)
        ),
        "explicit_non_signoff": bool(non_signoff),
        "formal_signoff_requested": bool(formal_signoff),
    }
    if not formal_signoff:
        status["signoff_gate_reasons"] = [
            "explicit_non_signoff"
            if non_signoff
            else "formal_signoff_not_requested"
        ]
        return status
    if not job_id:
        raise RuntimeError(
            "ISF-E3004 formal signoff quality gate failed: job_id_absent"
        )

    blockers = []
    if point_count < 6:
        blockers.append("fewer_than_six_points")
    if not numerical_quality_all_pass:
        blockers.append("spectre_numerical_quality_warning")
    if status["hbac_required_for_signoff"] and not hbac_secondary_all_pass:
        blockers.append("hbac_secondary_validation_failed")
    if not pm_magnitude_projection_all_pass:
        blockers.append("pm_magnitude_projection_not_valid")
    if blockers:
        raise RuntimeError(
            "ISF-E3004 formal signoff quality gate failed: {}".format(
                ",".join(blockers)
            )
        )
    status.update(
        {
            "run_kind": "signoff",
            "signoff": True,
            "signoff_gate_reasons": [],
        }
    )
    return status


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--job-id")
    export_mode = parser.add_mutually_exclusive_group()
    export_mode.add_argument(
        "--non-signoff",
        action="store_true",
        help="export replay/smoke evidence without applying the six-point signoff gate",
    )
    export_mode.add_argument(
        "--formal-signoff",
        action="store_true",
        help="explicitly request the six-point formal provenance and quality gate",
    )
    args = parser.parse_args(argv)

    engine_path = args.engine
    if engine_path is None and args.source_root is not None:
        engine_path = args.source_root / "dynamic_isf_closure.py"
    if engine_path is None:
        engine_path = script_path("dynamic_isf_closure_engine.py")
    engine_path = engine_path.resolve()
    if not engine_path.is_file():
        parser.error("dynamic ISF engine does not exist: {}".format(engine_path))
    spec = importlib.util.spec_from_file_location("dynamic_isf_closure_engine", str(engine_path))
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    from isf_tool import calculation as calc
    from isf_tool.core.physical_profile import (
        main_injection_trace,
        resolve_device_subckt_file,
        resolve_physical_profile,
    )

    cfg = legacy.load_config(args.config)
    profile = legacy.physical_profile(cfg)
    profile_info = resolve_physical_profile(
        cfg,
        config_path=str(args.config.resolve()),
        allow_legacy=False,
    )
    if args.job_id and not cfg.get("_physical_profile_explicit", False):
        raise RuntimeError(
            "formal export requires an explicit versioned physical_profile"
        )
    main_profile = profile["main"]
    main_traces = main_profile["traces"]
    operating_point_records = operating_point_metadata(cfg, legacy.point_name)
    summary_rows = rows(args.run_root / "summary.csv")
    runtime_spectre_logs = sorted(
        args.run_root.glob("**/psf/spectre.out")
    )
    runtime_numerical_quality = legacy.aggregate_spectre_quality(
        [("runtime", path) for path in runtime_spectre_logs]
    )
    export_status = classify_export(
        summary_rows,
        job_id=args.job_id,
        config=cfg,
        runtime_numerical_quality=runtime_numerical_quality,
        non_signoff=args.non_signoff,
        formal_signoff=args.formal_signoff,
    )
    vco_out = []
    replica_out = []
    workpoint_out = []
    raw_t0_spreads = []
    psf_provenance = []
    result = {"summary": summary_rows, "points": {}}

    for summary in summary_rows:
        summary.update(spectre_noise_evidence(summary))
        summary.update(enrich_summary(summary, offset_hz=float(cfg["offset_hz"]), pair_model="symmetric_independent"))
        point = summary["point"]
        summary["operating_parameters"] = copy.deepcopy(operating_point_records.get(point, {}).get("params", {}))
        case = args.run_root / point
        main_psf = case / "main_vco" / "psf"
        replica_psf = case / "replica" / "psf"
        for role, psf_dir in (("vco", main_psf), ("replica", replica_psf)):
            sim_done = psf_dir / ".simDone"
            spectre_out = psf_dir / "spectre.out"
            netlist = psf_dir.parent / "netlist" / "input.scs"
            execution_contract_path = psf_dir.parent / "run_environment.json"
            if (
                not sim_done.is_file()
                or not spectre_out.is_file()
                or not netlist.is_file()
            ):
                raise RuntimeError("incomplete {} PSF for {}: {}".format(role, point, psf_dir))
            log_text = spectre_out.read_text(encoding="utf-8", errors="replace")
            zero_errors = bool(re.search(r"spectre\s+completes\s+with\s+0\s+errors?", log_text, re.I))
            fatal = bool(re.search(r"\bfatal\b|^\s*error\b", log_text, re.I | re.M))
            if not zero_errors or fatal:
                raise RuntimeError("Spectre completion proof failed for {} {}".format(point, role))
            execution_contract = None
            if execution_contract_path.is_file():
                execution_contract = json.loads(
                    execution_contract_path.read_text(encoding="utf-8")
                )
            elif args.job_id:
                raise RuntimeError(
                    "ISF-E3002 formal export lacks run_environment.json for {} {}".format(
                        point, role
                    )
                )
            if (
                args.job_id
                and cfg.get("cadence_env")
                and not execution_contract.get("cadence_env_applied", False)
            ):
                raise RuntimeError(
                    "ISF-E3002 Cadence environment was not applied for {} {}".format(
                        point, role
                    )
                )
            psf_identity = tree_identity(psf_dir)
            provenance = {
                    "point": point,
                    "role": role,
                    "psf_dir": str(psf_dir.resolve()),
                    "sim_done_mtime": sim_done.stat().st_mtime,
                    "spectre_out_mtime": spectre_out.stat().st_mtime,
                    "spectre_out_sha256": hashlib.sha256(spectre_out.read_bytes()).hexdigest(),
                    "netlist_path": str(netlist.resolve()),
                    "netlist_sha256": hashlib.sha256(netlist.read_bytes()).hexdigest(),
                    "psf_tree_sha256": psf_identity["sha256"],
                    "psf_file_count": psf_identity["files"],
                    "psf_bytes": psf_identity["bytes"],
                    "zero_errors": True,
                }
            if execution_contract is not None:
                provenance.update(
                    {
                        "run_environment_path": str(
                            execution_contract_path.resolve()
                        ),
                        "run_environment_sha256": file_sha256(
                            execution_contract_path
                        ),
                        "cadence_env_applied": bool(
                            execution_contract.get("cadence_env_applied")
                        ),
                        "license_configured": bool(
                            execution_contract.get("license_configured")
                        ),
                    }
                )
            psf_provenance.append(provenance)
        harmonic_rows = rows(case / "harmonic_closure.csv")
        candidate_rows = rows(case / "phase_candidates.csv")
        workpoint = rows(case / "device_workpoint.csv")
        raw_origins = []
        for trace in dict.fromkeys(
            (
                main_traces["drain"],
                main_traces["gate"],
                main_traces["source"],
                main_traces["bulk"],
                main_traces["current"],
            )
        ):
            if trace == "0":
                continue
            raw_times, _raw_values = legacy.parse_real_wave(
                cfg, main_psf / "hb.td.pss_hb.tran", trace
            )
            if len(raw_times) == 0:
                raise RuntimeError("empty raw time axis for {} {}".format(point, trace))
            raw_origins.append(float(raw_times[0]))
        raw_t0_spreads.append(max(raw_origins) - min(raw_origins))
        max_k = int(cfg["max_harmonic"])
        f0 = legacy.fundamental(cfg, main_psf)
        output = legacy.main_output(cfg)
        positive, negative = (
            0j if node == "0" else legacy.nearest_complex(
                legacy.parse_complex_wave(cfg, main_psf / "hb.fd.pss_hb", node), f0)
            for node in (output["negative"], output["positive"])
        )
        carrier = positive - negative
        transfer = legacy.parse_hbxf(
            cfg, main_psf, main_injection_trace(profile), max_k
        )
        main_id_rows = legacy.parse_complex_wave(
            cfg, main_psf / "hb.fd.pss_hb", main_traces["current"]
        )

        for k in range(-max_k, max_k + 1):
            main_id = legacy.nearest_complex(main_id_rows, abs(k) * f0) if k >= 0 else 0j
            vco_out.append(
                {
                    "point": point,
                    "operating_point_id": summary["operating_point_id"],
                    "k": k,
                    "corner": summary["corner"],
                    "f0_Hz": f0,
                    "offset_Hz": cfg["offset_hz"],
                    "carrier_real_V": carrier.real,
                    "carrier_imag_V": carrier.imag,
                    "H_real_ohm": transfer[k].real,
                    "H_imag_ohm": transfer[k].imag,
                    "main_Id_real_A": main_id.real,
                    "main_Id_imag_A": main_id.imag,
                    "spectre_pm_asd_V_rtHz": summary["spectre_fn_pm_asd_V_rtHz"],
                    **noise_evidence_csv_fields(summary),
                    "second_device_predicted_pm_asd_V_rtHz": summary.get(
                        "dynamic_pm_asd_V_rtHz"
                    ),
                    "spectre_second_device_pm_asd_V_rtHz": summary.get(
                        "spectre_m0_fn_pm_asd_V_rtHz"
                    ),
                    "spectre_pair_pm_asd_V_rtHz": summary.get(
                        "spectre_pair_fn_pm_asd_V_rtHz"
                    ),
                    **{key: summary.get(key) for key in SUMMARY_QUALITY_FIELDS},
                }
            )

        mdc = {}
        mdyn = {}
        id_coeffs = {}
        for harmonic in harmonic_rows:
            k = int(harmonic["k"])
            mdc[k] = calc.phasor(float(harmonic["Mdc_mag_A_rtHz"]), float(harmonic["Mdc_phase_deg"]))
            mdyn[k] = calc.phasor(float(harmonic["Mdyn_mag_A_rtHz"]), float(harmonic["Mdyn_phase_deg"]))
            id_coeffs[k] = calc.phasor(float(harmonic["Id_mag_A"]), float(harmonic["Id_phase_deg"]))
            replica_out.append(
                {
                    "point": point,
                    "k": k,
                    "Id_real_A": id_coeffs[k].real,
                    "Id_imag_A": id_coeffs[k].imag,
                    "Mdc_real_A_rtHz": mdc[k].real,
                    "Mdc_imag_A_rtHz": mdc[k].imag,
                    "AM_A_rtHz": harmonic["AM_A_rtHz"],
                    "PM_A_rtHz": harmonic["PM_A_rtHz"],
                    "USB_A_rtHz": harmonic["USB_A_rtHz"],
                    "LSB_A_rtHz": harmonic["LSB_A_rtHz"],
                    "Mdyn_real_A_rtHz": mdyn[k].real,
                    "Mdyn_imag_A_rtHz": mdyn[k].imag,
                    "A_k": harmonic["A_k"],
                    "delta_k_deg": harmonic["delta_k_deg"],
                    "Ck_real": harmonic["Ck_real"],
                    "Ck_imag": harmonic["Ck_imag"],
                    "Dk_real": harmonic["Dk_real"],
                    "Dk_imag": harmonic["Dk_imag"],
                    "HBAC_norm_mag": harmonic["HBAC_norm_mag"],
                    "HBAC_norm_phase_deg": harmonic["HBAC_norm_phase_deg"],
                    "HBAC_vs_Mdyn_phase_deg": harmonic.get("HBAC_vs_Mdyn_phase_deg"),
                    "phase_candidate": summary["chosen_signs"],
                    "phase_score": summary["candidate_score"],
                    "sqrt_Sxi_A_rtHz": summary["sqrt_Sxi_A_rtHz"],
                }
            )

        gamma_branches = calc.carrier_referenced_gamma_branches(
            transfer, carrier, float(cfg["offset_hz"]), max_k
        )
        gamma_i = gamma_branches["i"]
        gamma_q = gamma_branches["q"]
        for index in range(257):
            wt = 2.0 * math.pi * index / 256.0
            mdc_value = value_at(mdc, wt)
            mdyn_value = value_at(mdyn, wt)
            gamma_i_value = value_at(gamma_i, wt)
            gamma_q_value = value_at(gamma_q, wt)
            workpoint_out.append(
                {
                    "point": point,
                    "sample": index,
                    "time_s": wt / (2.0 * math.pi * f0),
                    "wt_rad": wt,
                    "VG_V": interpolate_periodic(workpoint, "VG_V", wt),
                    "VD_V": interpolate_periodic(workpoint, "VD_V", wt),
                    "VS_V": interpolate_periodic(workpoint, "VS_V", wt),
                    "VB_V": interpolate_periodic(workpoint, "VB_V", wt),
                    "ID_main_A": interpolate_periodic(workpoint, "ID_drain_to_source_A", wt),
                    "ID_replica_A": value_at(id_coeffs, wt),
                    "m_DC_A_rtHz": mdc_value,
                    "m_dyn_A_rtHz": mdyn_value,
                    "isf": gamma_i_value,
                    "product": gamma_i_value * mdyn_value,
                    "isf_i": gamma_i_value,
                    "isf_q": gamma_q_value,
                    "product_i": gamma_i_value * mdyn_value,
                    "product_q": gamma_q_value * mdyn_value,
                }
            )

        computed = calc.calculate_point(
            transfer,
            mdc,
            mdyn,
            abs(carrier),
            float(cfg["offset_hz"]),
            float(summary["spectre_fn_pm_asd_V_rtHz"]),
            f0_hz=f0,
            carrier_phasor=carrier,
        )
        result["points"][point] = {
            "summary": [dict(summary, recomputed=computed)],
            "workpoint": [row for row in workpoint_out if row["point"] == point],
            "harmonics": harmonic_rows,
            "phase_candidates": candidate_rows,
        }

    vco_fields = list(vco_out[0])
    replica_fields = list(replica_out[0])
    workpoint_fields = list(workpoint_out[0])
    write(args.output / "vco_results.csv", vco_fields, vco_out)
    write(args.output / "replica_results.csv", replica_fields, replica_out)
    write(args.output / "workpoint.csv", workpoint_fields, workpoint_out)
    from isf_tool.core.preflight import _runtime_identity

    runtime_paths = {}
    for label, identity in _runtime_identity(str(args.config.resolve()))["files"].items():
        if not identity.get("path"):
            raise RuntimeError("runtime identity is missing a path: " + label)
        runtime_paths[label] = Path(identity["path"])
    # Preserve explicit development CLI overrides while sharing the complete
    # runtime-module inventory with preflight and normal bundled execution.
    runtime_paths["engine"] = engine_path
    runtime_paths["exporter"] = Path(__file__).resolve()
    pair_binding = cfg.get("maestro_pair_binding")
    if isinstance(pair_binding, dict):
        engine_template = pair_binding.get("engine_template")
        if engine_template:
            runtime_paths["engine_template"] = Path(engine_template).resolve()
        pair_manifest = args.run_root / ".isf_maestro_source_manifest.json"
        if pair_manifest.is_file():
            runtime_paths["maestro_source_manifest"] = pair_manifest.resolve()
        for role in ("vco", "flicker"):
            binding = pair_binding.get(role) or {}
            maestro_dir = binding.get("maestro_dir")
            if maestro_dir:
                sdb = Path(maestro_dir).resolve() / "maestro.sdb"
                state = Path(maestro_dir).resolve() / "active.state"
                if sdb.is_file():
                    runtime_paths["maestro_{}_sdb".format(role)] = sdb
                if state.is_file():
                    runtime_paths["maestro_{}_state".format(role)] = state
            history = binding.get("history")
            if history and Path(history).is_file():
                runtime_paths["maestro_{}_history".format(role)] = Path(
                    history
                ).resolve()
            seen_sources = set()
            source_index = 0
            history_manifest = binding.get("history_manifest") or {}
            for source in history_manifest.get("source_netlists", []):
                source_path = source.get("path")
                if not source_path:
                    continue
                source_path = Path(source_path).resolve()
                if source_path in seen_sources or not source_path.is_file():
                    continue
                seen_sources.add(source_path)
                runtime_paths[
                    "maestro_{}_source_{:03d}".format(role, source_index)
                ] = source_path
                source_index += 1
        private_flicker = cfg.get("flicker_template_netlist")
        if private_flicker and Path(private_flicker).is_file():
            runtime_paths["private_flicker_template"] = Path(
                private_flicker
            ).resolve()
    device_subckt_model = resolve_device_subckt_file(
        cfg.get("model_file"),
        profile,
        configured=cfg.get("device_subckt_file"),
    )
    if device_subckt_model and Path(device_subckt_model).resolve() != runtime_paths["model"]:
        runtime_paths["device_subckt_model"] = Path(
            device_subckt_model
        ).resolve()
    if cfg.get("cadence_env"):
        runtime_paths["cadence_env"] = Path(cfg["cadence_env"]).resolve()
    profile_source = profile_info.get("source")
    if isinstance(profile_source, str) and Path(profile_source).is_file():
        runtime_paths["physical_profile"] = Path(profile_source).resolve()
    for index, point in enumerate(cfg.get("points", [])):
        try:
            source_netlist = source_netlist_path(cfg, point)
        except ValueError as exc:
            raise RuntimeError(str(exc))
        runtime_paths["source_netlist_%03d" % index] = (
            Path(source_netlist)
        )
    runtime_identity = {
        label: {
            "path": str(path),
            "sha256": file_sha256(path),
        }
        for label, path in runtime_paths.items()
    }
    metadata = {
        "format": "canonical-two-tb-v2",
        "run_kind": export_status["run_kind"],
        "signoff": export_status["signoff"],
        "point_count": export_status["point_count"],
        "operating_points": operating_point_records,
        "formal_point_count_qualified": export_status[
            "formal_point_count_qualified"
        ],
        "numerical_quality_all_pass": export_status[
            "numerical_quality_all_pass"
        ],
        "summary_numerical_quality_all_pass": export_status[
            "summary_numerical_quality_all_pass"
        ],
        "runtime_numerical_quality_pass": export_status[
            "runtime_numerical_quality_pass"
        ],
        "runtime_numerical_quality_issues": export_status[
            "runtime_numerical_quality_issues"
        ],
        "hbac_secondary_all_pass": export_status[
            "hbac_secondary_all_pass"
        ],
        "pm_magnitude_projection_all_pass": export_status[
            "pm_magnitude_projection_all_pass"
        ],
        "hbac_required_for_signoff": export_status[
            "hbac_required_for_signoff"
        ],
        "signoff_gate_reasons": export_status["signoff_gate_reasons"],
        "offset_hz": float(cfg["offset_hz"]),
        "max_harmonic": int(cfg["max_harmonic"]),
        "canonical_noise_quantity": "ASD_A_per_sqrt_Hz",
        "source_noise_quantity": "PSD_A2_per_Hz",
        "psd_square_roots": 1,
        "current_direction": "drain_to_source",
        "carrier_amplitude": "differential_peak",
        "output_noise": "PM_SSB",
        "phase_selection": "HBNOISE_AM_PM_engineering_candidate",
        "hbac_role": "secondary_validation_only",
        "pac_role": "secondary_validation_only",
        "common_source_normalization": "Mdyn[k]=Dk*sqrt_Sxi",
        "isf_source": "carrier_referenced_signed_H_iq_v1",
        "isf_independent_of_noise_modulation": True,
        "time_origin": "shared_unshifted_VCO_period",
        "raw_t0_spread_s": max(raw_t0_spreads) if raw_t0_spreads else 0.0,
        "replica_drive_time_origin_s": 0.0,
        "manual_waveform_shift": False,
        "device_count": 2,
        "second_device_model": "symmetric_independent",
        "independent_device_combination": "power",
        "job_id": args.job_id,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "generated_at_epoch": time.time(),
        "source_run_root": str(args.run_root.resolve()),
        "engine": str(engine_path),
        "config": str(args.config.resolve()),
        "engine_sha256": hashlib.sha256(engine_path.read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "physical_profile_id": profile["profile_id"],
        "physical_profile_schema_version": profile["schema_version"],
        "physical_profile_sha256": cfg["_physical_profile_sha256"],
        "physical_profile_explicit": cfg["_physical_profile_explicit"],
        "runtime_identity": runtime_identity,
        "maestro_pair_binding": pair_binding,
        "psf_runs": psf_provenance,
    }
    if cfg.get("replica_source") == "generated":
        from isf_tool.core.generated_flicker import FLICKER_SCOPE
        metadata.update(replica_source="generated", flicker_scope=FLICKER_SCOPE,
                        target_noise_trace=main_traces["target_noise"],
                        peer_noise_trace=main_traces.get("peer_noise"))
    (args.output / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    result = enrich_result(result)
    (args.output / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    manifest_files = {}
    for name in (
        "vco_results.csv", "replica_results.csv", "workpoint.csv",
        "metadata.json", "result.json",
    ):
        path = args.output / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_files[name] = {"sha256": digest, "bytes": path.stat().st_size}
    manifest = {
        "schema_version": 1,
        "job_id": args.job_id,
        "generated_at": metadata["generated_at"],
        "source_run_root": metadata["source_run_root"],
        "files": manifest_files,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()
