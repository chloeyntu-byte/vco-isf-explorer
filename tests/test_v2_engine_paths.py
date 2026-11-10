"""Offline regression tests for engine-config path anchoring."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isf_tool import calculation as shared_calculation
from isf_tool.core.limits import MAX_PHASE_CANDIDATE_HARMONICS
from isf_tool.core.physical_profile import (
    LEGACY_PHYSICAL_PROFILE,
    dc_noise_trace,
    inspect_engine_contract,
    resolve_physical_profile,
)


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name, filename):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENGINE = _load_script("test_dynamic_isf_closure_engine", "dynamic_isf_closure_engine.py")
RUNNER = _load_script("test_cadence_dual_tb_runner", "cadence_dual_tb_runner.py")
EXPORTER = _load_script("test_export_audited_bundle", "export_audited_bundle.py")


@contextlib.contextmanager
def _working_directory(path):
    previous = Path.cwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(str(previous))


def _write_profile(path, injection_source="I4"):
    profile = copy.deepcopy(LEGACY_PHYSICAL_PROFILE)
    profile["main"]["injection"]["source"] = injection_source
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile), encoding="utf-8")
    return profile


def _base_config(paths):
    return {
        "offset_hz": 10000.0,
        "max_harmonic": 3,
        "hb_max_harmonic": 12,
        "main_max_sideband": 10,
        "points": [
            {"k": 0.6, "corner": "fast", "source_dir": "point_fast"}
        ],
        **paths,
    }


class EngineConfigPathTests(unittest.TestCase):
    def test_engine_iq_gamma_matches_shared_calculation(self):
        transfer = {
            -1: 0.3 + 0.2j,
            0: -0.1 + 0.4j,
            1: 0.7 - 0.5j,
        }
        carrier = 1.2 + 0.9j
        rotation_i = carrier.conjugate() / abs(carrier)
        rotation_q = -1j * rotation_i
        engine = {
            "i": ENGINE.exact_gamma_from_signed_h(
                transfer, rotation_i, abs(carrier), 1.0e4, 1
            ),
            "q": ENGINE.exact_gamma_from_signed_h(
                transfer, rotation_q, abs(carrier), 1.0e4, 1
            ),
        }
        shared = shared_calculation.carrier_referenced_gamma_branches(
            transfer, carrier, 1.0e4, 1
        )
        for branch in ("i", "q"):
            for harmonic in shared[branch]:
                self.assertAlmostEqual(
                    engine[branch][harmonic].real,
                    shared[branch][harmonic].real,
                    places=9,
                )
                self.assertAlmostEqual(
                    engine[branch][harmonic].imag,
                    shared[branch][harmonic].imag,
                    places=9,
                )

    def test_dynamic_dbc_uses_none_for_unavailable_values(self):
        self.assertIsNone(ENGINE.dBc_from_pm_asd(0.0, 1.0))
        self.assertIsNone(ENGINE.dBc_from_pm_asd(1.0, 0.0))
        self.assertIsNone(ENGINE.dBc_from_pm_asd(float("nan"), 1.0))
        self.assertIsNone(ENGINE.dBc_from_pm_asd(1.0, float("inf")))

        expected = 10.0 * math.log10(2.0) + 20.0 * (
            math.log10(1.0e-300) - math.log10(1.0e300)
        )
        actual = ENGINE.dBc_from_pm_asd(1.0e-300, 1.0e300)
        self.assertTrue(math.isfinite(actual))
        self.assertAlmostEqual(expected, actual)

    def test_dynamic_json_serialization_replaces_nonfinite_values(self):
        encoded = ENGINE._strict_json_dumps(
            {
                "available": 1.25,
                "missing": float("nan"),
                "nested": [float("inf"), float("-inf")],
            },
            sort_keys=True,
        )
        self.assertEqual(
            {"available": 1.25, "missing": None, "nested": [None, None]},
            json.loads(encoded),
        )

    def test_generic_hierarchy_and_split_model_are_auto_resolved(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source_root = root_path / "source"
            netlist = source_root / "nominal" / "vco" / "netlist" / "input.scs"
            netlist.parent.mkdir(parents=True)
            netlist.write_text(
                """simulator lang=spectre
subckt generic_stage VOUTN VOUTP VSUPPLY VCOMMON
    XDEV (VOUTN VOUTP VCOMMON VCOMMON) generic_nmos_wrapper l=1u w=2u nf=4 m=1
ends generic_stage
I_HBXF_XDEV (VOUTN VCOMMON) isource dc=0 type=dc
hb (VOUTN VOUTP) hb fundfreqs=[1G] maxharms=[12] annotate=status
hbnoise (VOUTN VOUTP) hbnoise sweeptype=relative relharmvec=[1] values=[10k] annotate=status
hbxf (VOUTN VOUTP) hbxf sweeptype=relative relharmvec=[1] values=[10k] annotate=status
""",
                encoding="utf-8",
            )
            model = root_path / "PDK_A" / "models" / "model_sections.lib"
            model.parent.mkdir(parents=True)
            model.write_text(
                'section typ\ninclude "device_model.scs"\nendsection typ\n',
                encoding="utf-8",
            )
            device_model = model.parent / "device_model.scs"
            device_model.write_text(
                """subckt generic_nmos_wrapper (1 2 3 4)
core (11 21 31 41) generic_nmos l=l w=w m=nf
ends generic_nmos_wrapper
""",
                encoding="utf-8",
            )
            spectre = root_path / "spectre"
            psf = root_path / "psf"
            spectre.write_text("", encoding="utf-8")
            psf.write_text("", encoding="utf-8")
            profile = copy.deepcopy(LEGACY_PHYSICAL_PROFILE)
            profile["profile_id"] = "generic-split-model-test-v1"
            profile["main"].update(
                {
                    "target_instance": "XDEV",
                    "instance_path": "XTOP.XSTAGE.XDEV",
                    "device_subckt": "generic_nmos_wrapper",
                    "terminals": {
                        "drain": "VOUTN",
                        "gate": "VOUTP",
                        "source": "VCOMMON",
                        "bulk": "VCOMMON",
                    },
                    "output": {
                        "positive": "VOUTP",
                        "negative": "VOUTN",
                    },
                    "drain_probe": {
                        "node": "XDEV_D",
                        "source": "V_XDEV_D",
                        "trace": "XTOP.XSTAGE.V_XDEV_D:p",
                    },
                    "injection": {
                        "source": "I_HBXF_XDEV",
                        "positive": "VOUTN",
                        "negative": "VCOMMON",
                    },
                    "traces": {
                        "gate": "VOUTP",
                        "drain": "VOUTN",
                        "source": "VCOMMON",
                        "bulk": "VCOMMON",
                        "current": "XTOP.XSTAGE.V_XDEV_D:p",
                        "target_noise": "XTOP.XSTAGE.XDEV.core",
                        "peer_noise": "XTOP.XSTAGE.XPEER.core",
                        "dc_noise": "M1.core",
                    },
                }
            )
            profile["replica"].update(
                {
                    "original_subckt": "generic_nmos_wrapper",
                    "generated_subckt": "generic_nmos_wrapper_dynprobe",
                    "device_parameters": "l=1u w=2u nf=4 m=1",
                    "model_sections": ["typ"],
                    "noise_trace": "MREP.core",
                    "intrinsic": {
                        "instance": "core",
                        "primitive": "generic_nmos",
                        "terminals": ["11", "21", "31", "41"],
                        "injection_source": "IINJ",
                    },
                }
            )
            config = _base_config(
                {
                    "source_root": str(source_root),
                    "model_file": str(model),
                    "spectre": str(spectre),
                    "psf": str(psf),
                    "physical_profile": profile,
                }
            )
            config["points"] = [
                {"k": 0.0, "corner": "typ", "source_dir": "nominal"}
            ]
            config_path = root_path / "engine.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            report = inspect_engine_contract(
                config,
                config_path=str(config_path),
                require_explicit=True,
                check_paths=True,
            )
            self.assertTrue(report["ok"], report)
            self.assertEqual(
                str(device_model.resolve()),
                report["resolved_paths"]["device_subckt_file"],
            )

            loaded = ENGINE.load_config(config_path)
            self.assertEqual(
                str(device_model.resolve()), loaded["_device_subckt_file"]
            )
            patched = root_path / "patched.scs"
            ENGINE.prepare_main_netlist(loaded, netlist, patched)
            patched_text = patched.read_text(encoding="utf-8")
            self.assertIn(
                "    V_XDEV_D (VOUTN XDEV_D) vsource dc=0 type=dc",
                patched_text,
            )
            self.assertIn(
                "save VOUTP VOUTN VOUTP VCOMMON VCOMMON XTOP.XSTAGE.V_XDEV_D:p",
                patched_text,
            )
            generated = ENGINE.injected_nmos_subckt(loaded)
            self.assertIn("subckt generic_nmos_wrapper_dynprobe", generated)
            self.assertIn("IINJ (11 31) isource", generated)
            inferred_profile = copy.deepcopy(profile)
            inferred_profile["main"]["traces"].pop("dc_noise")
            self.assertEqual("M1.core", dc_noise_trace(inferred_profile))

    def test_real_wave_parser_accepts_relative_frequency_axis(self):
        sample = '\n'.join(
            (
                '"relative frequency" "1.0000000000000000e+04"',
                '"out" "2.5000000000000000e-06"',
            )
        )
        with mock.patch.object(ENGINE, "psf_trace", return_value=sample):
            x, y = ENGINE.parse_real_wave({}, Path("unused"), "out")
        self.assertEqual([10000.0], x.tolist())
        self.assertEqual([2.5e-6], y.tolist())

    def test_one_point_smoke_cannot_claim_cross_point_trend(self):
        metrics = ENGINE.trend_metrics([{"dynamic_error_dB": 0.1}])
        self.assertEqual(1, metrics["point_count"])
        self.assertFalse(metrics["trend_qualified"])
        self.assertFalse(metrics["trend_match"])
        self.assertFalse(metrics["formal_point_count_qualified"])

    def test_smoke_point_defaults_to_first_and_supports_explicit_selector(self):
        points = [
            {"k": 0.2, "corner": "typ", "source_dir": "PDK_A_nominal"},
            {"k": 0.6, "corner": "fast", "source_dir": "PDK_B_variant"},
        ]
        config = {"points": points}
        self.assertEqual(points[:1], ENGINE.select_points(config, smoke=True))
        config["smoke_point"] = {"source_dir": "PDK_B_variant"}
        self.assertEqual(points[1:], ENGINE.select_points(config, smoke=True))
        self.assertEqual(
            points[1:], RUNNER.selected_points(ENGINE, config, smoke=True)
        )

    def test_point_name_is_stable_safe_and_collision_resistant(self):
        first = {
            "k": 0.61,
            "corner": "typ/../../x",
            "source_dir": "point_a",
        }
        reordered = {
            "source_dir": "point_a",
            "corner": "typ/../../x",
            "k": 0.61,
        }
        second = {
            "k": 0.64,
            "corner": "typ/../../x",
            "source_dir": "point_b",
        }

        first_name = ENGINE.point_name(first)
        self.assertEqual(first_name, ENGINE.point_name(dict(first, k="0.61")))
        self.assertEqual(first_name, ENGINE.point_name(reordered))
        self.assertNotEqual(first_name, ENGINE.point_name(second))
        self.assertRegex(first_name, r"^k_0p61_typ_x__[0-9a-f]{10}$")
        self.assertNotIn("/", first_name)
        self.assertNotIn("..", first_name)

        config = {
            "points": [first],
            "smoke_point": ENGINE._legacy_point_name(first),
        }
        self.assertEqual([first], ENGINE.select_points(config, smoke=True))

    def test_dynamic_phase_candidate_search_rejects_exponential_input(self):
        harmonics = range(1, MAX_PHASE_CANDIDATE_HARMONICS + 2)
        values = dict((item, 1.0) for item in harmonics)
        complex_values = dict(
            (item, 1.0 + 0.0j) for item in harmonics
        )

        with self.assertRaisesRegex(RuntimeError, "before running Spectre"):
            ENGINE.candidate_sets(
                complex_values, values, values, complex_values
            )

    def test_formal_export_quality_gate_fails_closed(self):
        smoke = [
            {
                "numerical_quality_pass": "True",
                "hbac_secondary_validation_pass": "False",
                "pm_magnitude_projection_valid": "True",
            }
        ]
        replay = EXPORTER.classify_export(smoke)
        self.assertEqual("replay", replay["run_kind"])
        self.assertFalse(replay["signoff"])
        smoke_with_job = EXPORTER.classify_export(
            smoke, job_id="traceable-smoke", non_signoff=True
        )
        self.assertEqual("replay", smoke_with_job["run_kind"])
        self.assertFalse(smoke_with_job["signoff"])
        self.assertEqual(
            ["explicit_non_signoff"],
            smoke_with_job["signoff_gate_reasons"],
        )
        traced_replay = EXPORTER.classify_export(
            smoke, job_id="traceable-default"
        )
        self.assertEqual("replay", traced_replay["run_kind"])
        self.assertEqual(
            ["formal_signoff_not_requested"],
            traced_replay["signoff_gate_reasons"],
        )
        with self.assertRaisesRegex(RuntimeError, "ISF-E3004"):
            EXPORTER.classify_export(
                smoke, job_id="formal-job", formal_signoff=True
            )

        six_clean = [
            {
                "numerical_quality_pass": "True",
                "hbac_secondary_validation_pass": "False",
                "pm_magnitude_projection_valid": "True",
            }
            for _index in range(6)
        ]
        formal = EXPORTER.classify_export(
            six_clean, job_id="formal-job", formal_signoff=True
        )
        self.assertEqual("signoff", formal["run_kind"])
        self.assertTrue(formal["signoff"])
        with self.assertRaisesRegex(RuntimeError, "job_id_absent"):
            EXPORTER.classify_export(six_clean, formal_signoff=True)
        invalid_pm = [dict(row, pm_magnitude_projection_valid="False") for row in six_clean]
        with self.assertRaisesRegex(RuntimeError, "pm_magnitude_projection_not_valid"):
            EXPORTER.classify_export(
                invalid_pm, job_id="formal-job", formal_signoff=True
            )
        with self.assertRaisesRegex(RuntimeError, "ISF-E3004"):
            EXPORTER.classify_export(
                six_clean,
                job_id="formal-job",
                config={"require_hbac_validation_for_signoff": True},
                formal_signoff=True,
            )
        with self.assertRaisesRegex(RuntimeError, "ISF-E3004"):
            EXPORTER.classify_export(
                six_clean,
                job_id="formal-job",
                runtime_numerical_quality={
                    "pass": False,
                    "issues": ["runtime:MODEL_RESULTS_DECLARED_INCORRECT"],
                },
                formal_signoff=True,
            )

    def test_runner_forwards_explicit_export_mode(self):
        fake_exporter = mock.Mock()
        with mock.patch.object(RUNNER, "load_module", return_value=fake_exporter):
            RUNNER.export_canonical(
                Path("engine.py"),
                Path("config.json"),
                Path("run"),
                Path("bundle"),
                job_id="formal-job",
                formal_signoff=True,
            )
        formal_args = fake_exporter.main.call_args.args[0]
        self.assertIn("--formal-signoff", formal_args)
        self.assertNotIn("--non-signoff", formal_args)

        fake_exporter.reset_mock()
        with mock.patch.object(RUNNER, "load_module", return_value=fake_exporter):
            RUNNER.export_canonical(
                Path("engine.py"),
                Path("config.json"),
                Path("run"),
                Path("bundle"),
                job_id="smoke-job",
                non_signoff=True,
            )
        replay_args = fake_exporter.main.call_args.args[0]
        self.assertIn("--non-signoff", replay_args)
        self.assertNotIn("--formal-signoff", replay_args)

    def test_spectre_quality_scanner_promotes_known_numerical_warnings(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "spectre.out"
            log.write_text(
                "WARNING: steady-state has not been detected\n"
                "spectre completes with 0 errors\n",
                encoding="utf-8",
            )
            quality = ENGINE.spectre_log_quality(log)
            self.assertFalse(quality["pass"])
            self.assertEqual("ISF-W3002", quality["code"])
            self.assertIn("HB_STEADY_STATE_NOT_DETECTED", quality["issues"])

    def test_missing_junction_forward_bias_is_not_hidden_by_zero_errors(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / 'spectre.out'
            for terminal in ('drain', 'source'):
                log.write_text('WARNING (CMI-2151): Missing bulk-' + terminal +
                               ' diode would be\n forward biased.\nspectre completes with 0 errors\n')
                quality = ENGINE.aggregate_spectre_quality([('thermal_main', log)])
                self.assertFalse(quality['pass'])
                self.assertIn('thermal_main:MODEL_MISSING_JUNCTION_FORWARD_BIAS', quality['issues'])
            log.write_text('WARNING (CMI-2666): rd is small\nWARNING (SPECTRE-17101): deprecated\n0 errors\n')
            self.assertTrue(ENGINE.spectre_log_quality(log)['pass'])

    def test_spectre_cache_requires_matching_versioned_identity(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            netlist_dir = root_path / "netlist"
            psf_dir = root_path / "psf"
            netlist_dir.mkdir()
            psf_dir.mkdir()
            input_path = netlist_dir / "input.scs"
            input_path.write_text("simulator lang=spectre\n", encoding="utf-8")
            (psf_dir / ".simDone").write_text("done\n", encoding="utf-8")
            (psf_dir / "spectre.out").write_text(
                "spectre completes with 0 errors\n", encoding="utf-8"
            )
            contract_path = root_path / "run_environment.json"
            contract_path.write_text(
                json.dumps({"sentinel": "legacy-run"}),
                encoding="utf-8",
            )
            original_contract = contract_path.read_bytes()
            config = {
                "spectre": "/cadence/bin/spectre",
                "cadence_env": "/project/cadence_env.sh",
            }

            with mock.patch.object(ENGINE.subprocess, "run") as execute:
                with self.assertRaisesRegex(RuntimeError, "legacy Spectre cache"):
                    ENGINE.run_spectre(config, netlist_dir)
            execute.assert_not_called()
            self.assertEqual(original_contract, contract_path.read_bytes())

            (psf_dir / ".simDone").unlink()
            completed = mock.Mock(returncode=0, stdout=b"")
            with mock.patch.object(
                ENGINE.subprocess, "run", return_value=completed
            ):
                status = ENGINE.run_spectre(config, netlist_dir)
            self.assertEqual("simulated", status)
            contract = json.loads(
                contract_path.read_text(encoding="utf-8")
            )
            self.assertEqual(2, contract["schema_version"])
            self.assertTrue(contract["cadence_env_applied"])
            self.assertTrue(contract["simulation_completed"])
            self.assertEqual("fresh_simulation", contract["execution_mode"])
            self.assertEqual(
                "/project/cadence_env.sh", contract["cadence_env"]
            )
            self.assertIn("key_sha256", contract["cache_identity"])

            sealed_contract = contract_path.read_bytes()
            with mock.patch.object(ENGINE.subprocess, "run") as execute:
                self.assertEqual(
                    "cached", ENGINE.run_spectre(config, netlist_dir)
                )
            execute.assert_not_called()
            self.assertEqual(sealed_contract, contract_path.read_bytes())
            changed_config = dict(config, offset_hz=20000.0)
            with mock.patch.object(ENGINE.subprocess, "run") as execute:
                with self.assertRaisesRegex(
                    RuntimeError, "identity does not match"
                ):
                    ENGINE.run_spectre(changed_config, netlist_dir)
            execute.assert_not_called()


            input_path.write_text("simulator lang=spectre\n// changed\n", encoding="utf-8")
            with mock.patch.object(ENGINE.subprocess, "run") as execute:
                with self.assertRaisesRegex(RuntimeError, "identity does not match"):
                    ENGINE.run_spectre(config, netlist_dir)
            execute.assert_not_called()

    def test_preflight_engine_and_runner_share_config_relative_paths_from_any_cwd(self):
        relative_paths = {
            "source_root": "../source-runs",
            "model_file": "PDK_A/models/device_model.scs",
            "spectre": "cadence/bin/spectre",
            "psf": "cadence/bin/psf",
            "physical_profile_path": "profiles/physical.json",
            "cadence_env": "cadence/cadence_env.sh",
            "ld_preload": "cadence/lib/libstdc++.so",
            "license_file": "cadence/license/license.dat",
        }
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            config_dir = root_path / "portable" / "configs"
            config_dir.mkdir(parents=True)
            profile_path = config_dir / relative_paths["physical_profile_path"]
            _write_profile(profile_path)
            config_path = config_dir / "engine.json"
            raw = _base_config(relative_paths)
            config_path.write_text(json.dumps(raw), encoding="utf-8")

            expected = {
                field: os.path.realpath(
                    os.path.join(str(config_dir), relative_paths[field])
                )
                for field in relative_paths
            }
            loaded_by_cwd = []
            runner_loaded_by_cwd = []
            for name in ("cwd-a", "cwd-b"):
                cwd = root_path / name
                cwd.mkdir()
                with _working_directory(cwd):
                    loaded_by_cwd.append(ENGINE.load_config(config_path))
                    relative_config = Path(
                        os.path.relpath(str(config_path), start=str(cwd))
                    )
                    resolved_path, loaded = RUNNER.load_engine_config(
                        ENGINE, relative_config
                    )
                    self.assertEqual(config_path.resolve(), resolved_path)
                    runner_loaded_by_cwd.append(loaded)

            for field, value in expected.items():
                self.assertEqual(value, loaded_by_cwd[0][field])
                self.assertEqual(value, loaded_by_cwd[1][field])
                self.assertEqual(value, runner_loaded_by_cwd[0][field])
                self.assertEqual(value, runner_loaded_by_cwd[1][field])

            preflight = inspect_engine_contract(
                raw,
                config_path=str(config_path),
                require_explicit=True,
                check_paths=False,
            )
            self.assertTrue(preflight["ok"], preflight)
            for field in ("source_root", "model_file", "spectre", "psf"):
                self.assertEqual(expected[field], preflight["resolved_paths"][field])
            profile_info = resolve_physical_profile(
                raw, config_path=str(config_path), allow_legacy=False
            )
            self.assertEqual(expected["physical_profile_path"], profile_info["source"])

    def test_absolute_engine_paths_are_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            config_dir = Path(root)
            profile_path = config_dir / "physical.json"
            _write_profile(profile_path)
            absolute_paths = {
                "source_root": "/portable/source-runs",
                "model_file": "/portable/PDK_A/models/device_model.scs",
                "spectre": "/portable/cadence/bin/spectre",
                "psf": "/portable/cadence/bin/psf",
                "physical_profile_path": str(profile_path),
                "cadence_env": "/portable/cadence/cadence_env.sh",
                "ld_preload": "/portable/cadence/lib/libstdc++.so",
                "license_file": "/portable/cadence/license/license.dat",
            }
            config_path = config_dir / "engine.json"
            config_path.write_text(
                json.dumps(_base_config(absolute_paths)), encoding="utf-8"
            )

            loaded = ENGINE.load_config(config_path)

            for field, value in absolute_paths.items():
                self.assertEqual(value, loaded[field])

    def test_report_uses_physical_profile_injection_source(self):
        with tempfile.TemporaryDirectory() as root:
            profile_path = Path(root) / "profile.json"
            profile = _write_profile(profile_path, injection_source="IPROFILE")
            config = {"offset_hz": 10000.0, "_physical_profile": profile}
            rows = [
                {
                    "point": "nominal",
                    "dc_error_dB": 0.1,
                    "delay_error_dB": 0.2,
                    "dynamic_error_dB": 0.3,
                    "pair_error_dB": 0.4,
                    "gamma_exact_equivalence_error_dB": 0.0,
                    "spectre_fn_pm_asd_V_rtHz": 1.0e-9,
                    "chosen_signs": "+,+,+",
                    "numerical_quality_pass": True,
                    "numerical_quality_code": "",
                    "hbac_secondary_validation_pass": True,
                    "hbac_secondary_validation_code": "",
                }
            ]

            report, _metrics = ENGINE.write_report(config, root, rows)
            text = report.read_text(encoding="utf-8")

            self.assertIn("Main HBXF source `IPROFILE`", text)
            self.assertNotIn("Main HBXF source I4", text)

    def test_source_dir_cannot_escape_source_root(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source_root = root_path / "selected-source"
            source_root.mkdir()
            outside = root_path / "outside-point"
            (outside / "vco" / "netlist").mkdir(parents=True)
            (outside / "vco" / "netlist" / "input.scs").write_text(
                "// adversarial source\n", encoding="utf-8"
            )
            model = root_path / "model.scs"
            spectre = root_path / "spectre"
            psf = root_path / "psf"
            for path in (model, spectre, psf):
                path.write_text("", encoding="utf-8")
            config = _base_config(
                {
                    "source_root": str(source_root),
                    "model_file": str(model),
                    "spectre": str(spectre),
                    "psf": str(psf),
                    "physical_profile": copy.deepcopy(
                        LEGACY_PHYSICAL_PROFILE
                    ),
                }
            )
            config["points"][0]["source_dir"] = "../outside-point"

            report = inspect_engine_contract(
                config,
                require_explicit=True,
                check_paths=True,
            )

            self.assertFalse(report["ok"])
            self.assertTrue(
                any(
                    item.get("field") == "points[0].source_dir"
                    and "outside source_root" in item.get("message", "")
                    for item in report["errors"]
                ),
                report,
            )
            with self.assertRaises(ValueError):
                ENGINE.source_case_path(config, config["points"][0])
            with self.assertRaises(ValueError):
                RUNNER.source_case_path(config, config["points"][0])

    def test_nested_vco_symlink_cannot_escape_source_root(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source_root = root_path / "selected-source"
            point_root = source_root / "inside-point"
            point_root.mkdir(parents=True)
            outside_vco = root_path / "outside-point" / "vco"
            (outside_vco / "netlist").mkdir(parents=True)
            (outside_vco / "netlist" / "input.scs").write_text(
                "// nested-symlink adversarial source\n",
                encoding="utf-8",
            )
            (point_root / "vco").symlink_to(
                outside_vco, target_is_directory=True
            )
            model = root_path / "model.scs"
            spectre = root_path / "spectre"
            psf = root_path / "psf"
            for path in (model, spectre, psf):
                path.write_text("", encoding="utf-8")
            config = _base_config(
                {
                    "source_root": str(source_root),
                    "model_file": str(model),
                    "spectre": str(spectre),
                    "psf": str(psf),
                    "physical_profile": copy.deepcopy(
                        LEGACY_PHYSICAL_PROFILE
                    ),
                }
            )
            config["points"][0]["source_dir"] = "inside-point"
            point = config["points"][0]

            report = inspect_engine_contract(
                config,
                require_explicit=True,
                check_paths=True,
            )

            self.assertFalse(report["ok"])
            self.assertTrue(
                any(
                    item.get("field") == "points[0].source_dir"
                    and "source input resolves outside" in item.get(
                        "message", ""
                    )
                    for item in report["errors"]
                ),
                report,
            )
            for resolver in (
                ENGINE.source_case_path,
                lambda cfg, selected: ENGINE.source_input_path(
                    cfg, selected, "vco/netlist/input.scs"
                ),
                RUNNER.source_case_path,
                RUNNER.source_netlist_path,
                EXPORTER.source_netlist_path,
            ):
                with self.assertRaises(ValueError):
                    resolver(config, point)

    def test_nested_dc_noise_inputs_cannot_escape_source_root(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source_root = root_path / "selected-source"
            point_root = source_root / "inside-point"
            point_root.mkdir(parents=True)
            outside = root_path / "outside-point"
            outside.mkdir()
            outside_csv = outside / "iflicker_samples.csv"
            outside_csv.write_text("wt_rad\n0\n", encoding="utf-8")
            (point_root / "iflicker_samples.csv").symlink_to(outside_csv)
            outside_iflicker = outside / "iflicker"
            (outside_iflicker / "sample_01" / "psf").mkdir(parents=True)
            (
                outside_iflicker
                / "sample_01"
                / "psf"
                / "noise.noise"
            ).write_text("noise\n", encoding="utf-8")
            (point_root / "iflicker").symlink_to(
                outside_iflicker, target_is_directory=True
            )
            config = {
                "source_root": str(source_root),
            }
            point = {"source_dir": "inside-point"}

            for relative_path in (
                "iflicker_samples.csv",
                "iflicker/sample_01/psf/noise.noise",
            ):
                with self.assertRaises(ValueError):
                    ENGINE.source_input_path(
                        config, point, relative_path
                    )


if __name__ == "__main__":
    unittest.main()
