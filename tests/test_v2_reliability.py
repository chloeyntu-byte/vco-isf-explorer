"""Offline reliability tests for the V2 workflow capabilities."""

from __future__ import unicode_literals

import copy
import inspect
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from isf_tool.core import errors as errors_module
from isf_tool.core import run_state as run_state_module
from isf_tool.core import service as service_module
from isf_tool.core.adapters import ProtocolAdapter
from isf_tool.core.error_catalog import load_error_catalog
from isf_tool.core.errors import (
    AdapterExecutionError,
    CoreError,
    PreflightError,
    ResumeError,
    ValidationError,
)
from isf_tool.core.limits import MAX_PHASE_CANDIDATE_HARMONICS
from isf_tool.core.preflight import (
    _default_commands,
    _spectre_identity,
    _target_directory_status,
    detect_environment,
    preflight,
)
from isf_tool.core.physical_profile import (
    LEGACY_PHYSICAL_PROFILE,
    inspect_engine_contract,
)
from isf_tool.core.run_state import RunState, STATE_FILENAME
from isf_tool.core.orchestrator import DualTBOrchestrator
from isf_tool.core.service import ToolService

try:
    from .synthetic_fixture import write_canonical_bundle
except ImportError:  # unittest discovery loads test modules as top-level names
    from synthetic_fixture import write_canonical_bundle


def _make_canonical_bundle(path):
    os.makedirs(path)
    contents = {
        "vco_results.csv": "point\n",
        "replica_results.csv": "point\n",
        "metadata.json": "{}\n",
    }
    for name, value in contents.items():
        with io.open(os.path.join(path, name), "w", encoding="utf-8") as stream:
            stream.write(value)


def _run_canonical_preflight(root):
    return preflight(
        {
            "project_root": root,
            "discovery": {
                "roots": [root],
                "max_depth": 4,
                "max_results": 20,
            },
            "input_policy": {
                "preferred": "canonical",
                "allowed": ["canonical"],
                "allow_degraded": False,
            },
            "execution": {"dry_run": True},
        },
        workspace=root,
    )


def _check_by_id(report, check_id):
    matches = [item for item in report["checks"] if item["id"] == check_id]
    if len(matches) != 1:
        raise AssertionError(
            "expected one %r check, found %d" % (check_id, len(matches))
        )
    return matches[0]


class ErrorCatalogReliabilityTests(unittest.TestCase):
    def test_catalog_codes_are_unique_and_cover_every_core_error_default(self):
        entries = load_error_catalog(refresh=True)["entries"]
        codes = [entry["code"] for entry in entries]

        self.assertTrue(codes)
        self.assertEqual(len(codes), len(set(codes)), codes)

        core_error_classes = [
            value
            for _name, value in inspect.getmembers(errors_module, inspect.isclass)
            if value.__module__ == errors_module.__name__
            and issubclass(value, CoreError)
        ]
        defaults = dict(
            (error_class.__name__, error_class.default_code)
            for error_class in core_error_classes
        )
        missing = dict(
            (name, code) for name, code in defaults.items() if code not in codes
        )
        self.assertEqual({}, missing)

    def test_damaged_catalog_never_masks_original_error(self):
        from isf_tool.core import error_catalog

        error = AdapterExecutionError(
            "original simulator failure",
            code="ISF-E4005",
            details={"returncode": 9},
        )
        with mock.patch.object(
            error_catalog, "error_entry", side_effect=ValueError("bad catalog")
        ):
            payload = error.to_dict()
        self.assertEqual("ISF-E4005", payload["code"])
        self.assertEqual("original simulator failure", payload["message"])
        self.assertEqual(9, payload["details"]["returncode"])


class PreflightReliabilityTests(unittest.TestCase):
    def test_bundled_export_command_matches_replay_and_formal_contracts(self):
        replay = _default_commands("/analysis/python", "replay")["extract_command"]
        formal = _default_commands("/analysis/python", "signoff")["extract_command"]

        self.assertIn("--non-signoff", replay)
        self.assertNotIn("--formal-signoff", replay)
        self.assertIn("--formal-signoff", formal)
        self.assertNotIn("--non-signoff", formal)

    def test_output_target_check_is_read_only_and_fails_for_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            future = os.path.join(root, "future", "run")
            status = _target_directory_status(future)
            self.assertTrue(status["ok"])
            self.assertFalse(os.path.exists(future))

            blocking_file = os.path.join(root, "not-a-directory")
            with io.open(blocking_file, "w", encoding="utf-8") as stream:
                stream.write("x")
            blocked = _target_directory_status(blocking_file)
            self.assertFalse(blocked["ok"])
            self.assertIn("not a directory", blocked["detail"])

    def test_active_environment_probes_require_fresh_live_consent(self):
        with tempfile.TemporaryDirectory() as root:
            bridge = os.path.join(root, "cadence_env.sh")
            with io.open(bridge, "w", encoding="utf-8") as stream:
                stream.write("exit 99\n")
            config = {
                "profiles": {
                    "environment": {
                        "cadence_env": bridge,
                        "python": "/bin/true",
                        "executables": {"spectre": "/bin/true"},
                    }
                },
                "execution": {"dry_run": False, "allow_execute": False},
            }
            with mock.patch(
                "isf_tool.core.preflight._probe_cadence_env",
                side_effect=AssertionError("Cadence env must not be sourced"),
            ), mock.patch(
                "isf_tool.core.preflight._probe_analysis_python",
                side_effect=AssertionError("configured Python must not run"),
            ):
                environment = detect_environment(config, workspace=root)
            self.assertFalse(environment["active_probes"])
            self.assertEqual("deferred", environment["cadence_env_probe"])

            with mock.patch(
                "isf_tool.core.preflight.subprocess.run",
                side_effect=AssertionError("Spectre identity probe must not run"),
            ):
                identity = _spectre_identity("/bin/true")
            self.assertTrue(identity["deferred"])

            cadence_probe = mock.Mock(return_value={})
            python_probe = mock.Mock(return_value=None)
            config["execution"]["allow_execute"] = True
            with mock.patch(
                "isf_tool.core.preflight._probe_cadence_env", cadence_probe
            ), mock.patch(
                "isf_tool.core.preflight._probe_analysis_python", python_probe
            ):
                environment = detect_environment(config, workspace=root)
            self.assertTrue(environment["active_probes"])
            cadence_probe.assert_called_once_with(os.path.realpath(bridge))
            self.assertTrue(python_probe.called)

    def test_missing_engine_reports_physical_profile_as_config_dependency(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = os.path.join(root, "canonical")
            _make_canonical_bundle(bundle)
            report = preflight(
                {
                    "source_mode": "manual",
                    "project_root": root,
                    "manual_import": {"bundle_path": bundle},
                    "input_policy": {
                        "preferred": "canonical",
                        "allowed": ["canonical"],
                    },
                    "execution": {
                        "dry_run": False,
                        "allow_execute": False,
                        "adapter_mode": "bundled",
                    },
                },
                workspace=root,
            )
            physical = _check_by_id(report, "physical_profile")
            self.assertEqual("warn", physical["status"])
            self.assertEqual("ISF-W1103", physical["code"])
            self.assertEqual("config_path", physical["field"])
            self.assertIn("physical_profile_path", physical["detail"])
            self.assertIn("config_path", report["required_user_fields"])
            self.assertNotIn("physical_profile", report["required_user_fields"])

    def test_execution_flags_require_real_booleans(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(
                ValidationError, "execution.allow_execute must be a boolean"
            ):
                preflight(
                    {
                        "project_root": root,
                        "discovery": {"roots": [root]},
                        "execution": {
                            "dry_run": False,
                            "allow_execute": "false",
                        },
                    },
                    workspace=root,
                )

    def test_resume_and_degraded_gates_require_real_booleans(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(
                ValidationError, "execution.resume_enabled must be a boolean"
            ):
                preflight(
                    {
                        "project_root": root,
                        "discovery": {"roots": [root]},
                        "execution": {"resume_enabled": "false"},
                    },
                    workspace=root,
                )
            with self.assertRaisesRegex(
                ValidationError,
                "input_policy.allow_degraded must be a boolean",
            ):
                preflight(
                    {
                        "project_root": root,
                        "discovery": {"roots": [root]},
                        "input_policy": {"allow_degraded": "false"},
                    },
                    workspace=root,
                )

    def test_unique_canonical_bundle_is_selected_automatically(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = os.path.join(root, "only_bundle")
            _make_canonical_bundle(bundle)

            report = _run_canonical_preflight(root)
            inputs = report["detected"]["inputs"]
            chosen = report["detected"]["chosen_input"]

            self.assertTrue(report["ok"])
            self.assertEqual(
                {"vco": "selected", "replica": "selected"},
                inputs["selection"],
            )
            self.assertEqual(os.path.realpath(bundle), inputs["selected"]["vco"]["path"])
            self.assertEqual(
                os.path.realpath(bundle), inputs["selected"]["replica"]["path"]
            )
            self.assertEqual("canonical", chosen["kind"])
            self.assertEqual("auto", chosen["source"])
            self.assertEqual(os.path.realpath(bundle), chosen["path"])
            self.assertEqual("pass", _check_by_id(report, "input_resolution")["status"])

    def test_equal_ranked_canonical_candidates_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            _make_canonical_bundle(os.path.join(root, "candidate_a"))
            _make_canonical_bundle(os.path.join(root, "candidate_b"))

            report = _run_canonical_preflight(root)
            inputs = report["detected"]["inputs"]
            resolution = _check_by_id(report, "input_resolution")

            self.assertFalse(report["ok"])
            self.assertEqual(
                {"vco": "ambiguous", "replica": "ambiguous"},
                inputs["selection"],
            )
            self.assertIsNone(report["detected"]["chosen_input"])
            self.assertEqual("fail", resolution["status"])
            self.assertEqual("ISF-E1103", resolution["code"])
            self.assertIn("testbenches", report["required_user_fields"])

    def test_live_engine_requires_explicit_physical_profile(self):
        engine = {
            "source_root": "/tmp/source",
            "points": [{"source_dir": "point"}],
            "model_file": "/tmp/model.scs",
            "spectre": "/bin/true",
            "psf": "/bin/true",
            "offset_hz": 10000.0,
            "max_harmonic": 3,
            "hb_max_harmonic": 12,
            "main_max_sideband": 10,
        }
        report = inspect_engine_contract(
            engine, require_explicit=True, check_paths=False
        )
        self.assertFalse(report["ok"])
        self.assertTrue(
            any(
                item.get("field") == "physical_profile"
                for item in report["errors"]
            )
        )

        engine["physical_profile"] = LEGACY_PHYSICAL_PROFILE
        report = inspect_engine_contract(
            engine,
            app_config={
                "analysis": {
                    "offset": {"hz": 10000.0},
                    "max_harmonic": 3,
                }
            },
            require_explicit=True,
            check_paths=False,
        )
        self.assertTrue(report["ok"], report)

    def test_engine_contract_rejects_exponential_phase_candidate_count(self):
        engine = {
            "source_root": "/tmp/source",
            "points": [{"source_dir": "point"}],
            "model_file": "/tmp/model.scs",
            "spectre": "/bin/true",
            "psf": "/bin/true",
            "offset_hz": 10000.0,
            "max_harmonic": MAX_PHASE_CANDIDATE_HARMONICS + 1,
            "hb_max_harmonic": 24,
            "main_max_sideband": 20,
            "physical_profile": LEGACY_PHYSICAL_PROFILE,
        }

        report = inspect_engine_contract(
            engine,
            require_explicit=True,
            check_paths=False,
        )

        self.assertFalse(report["ok"])
        errors = [
            item
            for item in report["errors"]
            if item.get("field") == "max_harmonic"
        ]
        self.assertEqual(1, len(errors), report)
        self.assertEqual(
            MAX_PHASE_CANDIDATE_HARMONICS, errors[0]["maximum"]
        )

    def test_physical_profile_direction_and_source_files_fail_closed(self):
        reversed_profile = copy.deepcopy(LEGACY_PHYSICAL_PROFILE)
        reversed_profile["main"]["injection"]["positive"] = "VSS"
        reversed_profile["main"]["injection"]["negative"] = "OUTP"
        report = inspect_engine_contract(
            {
                "source_root": "/tmp/source",
                "points": [{"source_dir": "point"}],
                "model_file": "/tmp/model.scs",
                "spectre": "/bin/true",
                "psf": "/bin/true",
                "offset_hz": 10000.0,
                "max_harmonic": 3,
                "hb_max_harmonic": 12,
                "main_max_sideband": 10,
                "physical_profile": reversed_profile,
            },
            require_explicit=True,
            check_paths=False,
        )
        self.assertFalse(report["ok"])
        self.assertTrue(
            any(
                item.get("field") == "physical_profile"
                for item in report["errors"]
            ),
            report,
        )

        with tempfile.TemporaryDirectory() as root:
            source_root = os.path.join(root, "source")
            netlist_dir = os.path.join(
                source_root, "point", "vco", "netlist"
            )
            os.makedirs(netlist_dir)
            model = os.path.join(root, "model.scs")
            spectre = os.path.join(root, "spectre")
            psf = os.path.join(root, "psf")
            for path in (
                os.path.join(netlist_dir, "input.scs"),
                model,
                spectre,
                psf,
            ):
                with io.open(path, "w", encoding="utf-8") as stream:
                    stream.write("")
            engine = {
                "source_root": source_root,
                "points": [{"source_dir": "point"}],
                "model_file": model,
                "spectre": spectre,
                "psf": psf,
                "offset_hz": 10000.0,
                "max_harmonic": 3,
                "hb_max_harmonic": 12,
                "main_max_sideband": 10,
                "physical_profile": copy.deepcopy(
                    LEGACY_PHYSICAL_PROFILE
                ),
            }
            report = inspect_engine_contract(
                engine,
                require_explicit=True,
                check_paths=True,
            )
        self.assertFalse(report["ok"])
        fields = set(item.get("field") for item in report["errors"])
        self.assertIn("target_instance", fields)
        self.assertIn("replica.original_subckt", fields)


class ServiceTrustBoundaryTests(unittest.TestCase):
    def test_execution_consent_is_never_persisted_or_restored(self):
        with tempfile.TemporaryDirectory() as workspace:
            service = ToolService(workspace=workspace)
            saved = service.save_config(
                {
                    "execution": {
                        "dry_run": False,
                        "allow_execute": True,
                    }
                }
            )
            self.assertFalse(saved["config"]["execution"]["allow_execute"])
            with io.open(service.config_path, "r", encoding="utf-8") as stream:
                persisted = json.load(stream)
            self.assertFalse(persisted["execution"]["allow_execute"])

            persisted["execution"]["allow_execute"] = True
            with io.open(service.config_path, "w", encoding="utf-8") as stream:
                json.dump(persisted, stream)
            loaded = service.load_config()

        self.assertFalse(loaded["execution"]["allow_execute"])

    def test_any_start_is_atomically_blocked_by_an_active_job(self):
        live_config = {
            "execution": {
                "dry_run": False,
                "allow_execute": True,
                "background": False,
            }
        }
        with tempfile.TemporaryDirectory() as workspace:
            service = ToolService(workspace=workspace)
            service._jobs["already-running"] = {
                "id": "already-running",
                "status": "running",
            }
            with mock.patch.object(
                service_module,
                "resolve_config",
                return_value=copy.deepcopy(live_config),
            ), mock.patch.object(
                service_module,
                "run_preflight",
                return_value={
                    "ok": True,
                    "checks": [],
                    "required_user_fields": [],
                    "resolved_config": copy.deepcopy(live_config),
                },
            ), mock.patch.object(
                DualTBOrchestrator,
                "run",
                side_effect=AssertionError("orchestrator must not run"),
            ):
                with self.assertRaisesRegex(
                    ValidationError, "already queued or running"
                ) as caught:
                    service.start_run({"background": False})

        self.assertEqual(
            ["already-running"], caught.exception.details["active_job_ids"]
        )

    def test_job_reservation_precedes_shared_run_config_write(self):
        with tempfile.TemporaryDirectory() as workspace:
            service = ToolService(workspace=workspace)
            shared_run_root = os.path.join(workspace, "shared-run")
            resolved_path = os.path.join(
                shared_run_root, ".isf_resolved_config.json"
            )
            original_atomic_json = ToolService._atomic_json
            rejected = []

            def intercept(path, value):
                if path == resolved_path and not rejected:
                    rejected.append(True)
                    with self.assertRaisesRegex(
                        ValidationError, "already queued or running"
                    ):
                        service.start_run(
                            {
                                "background": False,
                                "run_root": shared_run_root,
                                "config": {"project_name": "rejected"},
                            }
                        )
                return original_atomic_json(path, value)

            def fake_run(orchestrator, job_id, payload, emit=None):
                return {"summary": [], "points": {}}

            with mock.patch.object(
                service, "_atomic_json", side_effect=intercept
            ), mock.patch.object(DualTBOrchestrator, "run", new=fake_run):
                accepted_id = service.start_run(
                    {
                        "background": False,
                        "run_root": shared_run_root,
                        "config": {"project_name": "accepted"},
                    }
                )
            with io.open(resolved_path, "r", encoding="utf-8") as stream:
                resolved = json.load(stream)

        self.assertTrue(rejected)
        self.assertEqual("accepted", resolved["project_name"])
        self.assertEqual("succeeded", service.get_job(accepted_id)["status"])

    def test_background_worker_start_failure_releases_single_flight(self):
        failing_thread = mock.Mock()
        failing_thread.start.side_effect = RuntimeError("synthetic thread failure")

        def fake_run(orchestrator, job_id, payload, emit=None):
            return {"summary": [], "points": {}}

        with tempfile.TemporaryDirectory() as workspace:
            service = ToolService(workspace=workspace)
            with mock.patch.object(
                service_module.threading,
                "Thread",
                return_value=failing_thread,
            ):
                with self.assertRaisesRegex(
                    ValidationError, "cannot start background worker"
                ):
                    service.start_run({"background": True})
            with service._lock:
                self.assertEqual([], service._active_job_ids_locked())
                self.assertEqual({}, service._threads)
                self.assertEqual(
                    ["failed"],
                    [job["status"] for job in service._jobs.values()],
                )
            with mock.patch.object(DualTBOrchestrator, "run", new=fake_run):
                next_job = service.start_run({"background": False})

        self.assertEqual("succeeded", service.get_job(next_job)["status"])

    def test_resume_is_blocked_by_any_active_job_before_external_work(self):
        with tempfile.TemporaryDirectory() as workspace:
            service = ToolService(workspace=workspace)
            service._jobs.update(
                {
                    "failed-a": {
                        "id": "failed-a",
                        "status": "failed",
                    },
                    "already-running": {
                        "id": "already-running",
                        "status": "queued",
                    },
                }
            )
            with self.assertRaisesRegex(
                ResumeError, "already queued or running"
            ) as caught:
                service.resume_job("failed-a")

        self.assertEqual(
            ["already-running"], caught.exception.details["active_job_ids"]
        )

    def test_invalid_saved_resume_flag_fails_closed(self):
        with tempfile.TemporaryDirectory() as workspace:
            service = ToolService(workspace=workspace)
            service._jobs["failed-a"] = {
                "id": "failed-a",
                "status": "failed",
                "_request": {},
                "_config_snapshot": {
                    "execution": {"resume_enabled": "false"}
                },
            }
            with self.assertRaisesRegex(ResumeError, "resume is disabled"):
                service.resume_job("failed-a")

    def test_resume_persist_failure_restores_terminal_job(self):
        with tempfile.TemporaryDirectory() as workspace:
            run_root = os.path.join(workspace, "run")
            os.makedirs(run_root)
            with io.open(
                os.path.join(run_root, STATE_FILENAME), "w", encoding="utf-8"
            ) as stream:
                stream.write("{}\n")
            service = ToolService(workspace=workspace)
            service._jobs["failed-a"] = {
                "id": "failed-a",
                "status": "failed",
                "background": True,
                "result": None,
                "_request": {"run_root": run_root, "background": True},
                "_config_snapshot": {
                    "execution": {
                        "dry_run": True,
                        "resume_enabled": True,
                    }
                },
            }
            with mock.patch.object(
                service,
                "_persist_job",
                side_effect=ValidationError("synthetic persist failure"),
            ):
                with self.assertRaisesRegex(
                    ValidationError, "synthetic persist failure"
                ):
                    service.resume_job("failed-a")
            with service._lock:
                self.assertEqual("failed", service._jobs["failed-a"]["status"])
                self.assertEqual([], service._active_job_ids_locked())
                self.assertEqual({}, service._threads)

    def test_resume_thread_start_failure_restores_terminal_job(self):
        failing_thread = mock.Mock()
        failing_thread.start.side_effect = RuntimeError("synthetic thread failure")
        with tempfile.TemporaryDirectory() as workspace:
            run_root = os.path.join(workspace, "run")
            os.makedirs(run_root)
            with io.open(
                os.path.join(run_root, STATE_FILENAME), "w", encoding="utf-8"
            ) as stream:
                stream.write("{}\n")
            service = ToolService(workspace=workspace)
            service._jobs["failed-a"] = {
                "id": "failed-a",
                "status": "failed",
                "progress": 50.0,
                "message": "synthetic failure",
                "created_at": "2026-01-01T00:00:00Z",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "updated_at": "2026-01-01T00:00:01Z",
                "background": True,
                "events": [],
                "result": None,
                "error": {"code": "ISF-E9000", "message": "synthetic"},
                "_request": {"run_root": run_root, "background": True},
                "_config_snapshot": {
                    "execution": {
                        "dry_run": True,
                        "resume_enabled": True,
                    }
                },
            }
            with mock.patch.object(
                service_module.threading,
                "Thread",
                return_value=failing_thread,
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic thread failure"):
                    service.resume_job("failed-a")
            with service._lock:
                self.assertEqual("failed", service._jobs["failed-a"]["status"])
                self.assertEqual([], service._active_job_ids_locked())
                self.assertEqual({}, service._threads)

    def test_manual_import_is_always_replay_non_signoff(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = write_canonical_bundle(
                os.path.join(root, "synthetic_canonical"), signoff=True
            )
            workspace = os.path.join(root, "workspace")
            service = ToolService(workspace=workspace)
            response = service.import_as_job(
                {"path": fixture, "format": "dual_tb", "signoff": True}
            )
            result = service.get_result(response["job_id"])

        self.assertEqual("replay", result["run_kind"])
        self.assertFalse(result["signoff"])
        self.assertIsNone(result["provenance"])
        self.assertFalse(result["dual_tb"]["signoff"])
        self.assertEqual("replay", result["dual_tb"]["metadata"]["run_kind"])
        self.assertFalse(result["dual_tb"]["metadata"]["signoff"])

    def test_resolved_config_is_not_shallow_overwritten_by_request(self):
        captured = {}

        def fake_run(orchestrator, job_id, payload, emit=None):
            captured["config"] = orchestrator.config
            captured["payload"] = payload
            return {"summary": [], "points": {}}

        config = {
            "execution": {
                "dry_run": True,
                "background": False,
                "resume_enabled": False,
                "adapters": {
                    "vco": {
                        "kind": "protocol",
                        "submit_command": ["submit", "{job_id}"],
                    }
                },
            },
            "mapping": {"target_device": "M1"},
        }
        with tempfile.TemporaryDirectory() as workspace:
            service = ToolService(workspace=workspace, config=config)
            with mock.patch.object(DualTBOrchestrator, "run", new=fake_run):
                service.start_run(
                    {
                        "background": False,
                        "config": {
                            "analysis": {"max_harmonic": 5},
                            "execution": {"dry_run": True},
                        },
                    }
                )

        self.assertEqual(
            "protocol",
            captured["config"]["execution"]["adapters"]["vco"]["kind"],
        )
        self.assertEqual("M1", captured["config"]["mapping"]["target_device"])
        self.assertEqual(5, captured["config"]["analysis"]["max_harmonic"])
        self.assertNotIn("config", captured["payload"])

    def test_top_level_live_overrides_are_sealed_before_preflight(self):
        captured = {}

        def fake_preflight(config, workspace=None):
            captured["preflight"] = copy.deepcopy(config)
            return {
                "ok": True,
                "checks": [],
                "required_user_fields": [],
            }

        def fake_run(orchestrator, job_id, payload, emit=None):
            captured["orchestrator"] = copy.deepcopy(orchestrator.config)
            captured["payload"] = copy.deepcopy(payload)
            return {"summary": [], "points": {}}

        config = {
            "execution": {
                "dry_run": True,
                "allow_execute": False,
                "background": False,
                "resume_enabled": False,
            }
        }
        with tempfile.TemporaryDirectory() as workspace:
            service = ToolService(workspace=workspace, config=config)
            with mock.patch.object(
                service_module, "run_preflight", new=fake_preflight
            ), mock.patch.object(DualTBOrchestrator, "run", new=fake_run):
                service.start_run(
                    {
                        "background": False,
                        "dry_run": False,
                        "allow_execute": True,
                        "run_kind": "signoff",
                        "testbenches": {
                            "vco": {"name": "vco"},
                            "replica": {"name": "replica"},
                        },
                    }
                )

        for key in ("preflight", "orchestrator"):
            self.assertFalse(
                captured[key]["execution"]["dry_run"], captured[key]
            )
            self.assertTrue(
                captured[key]["execution"]["allow_execute"], captured[key]
            )
            self.assertEqual("signoff", captured[key]["run_kind"])
        self.assertEqual(
            captured["preflight"]["execution"],
            captured["orchestrator"]["execution"],
        )
        for key in (
            "dry_run",
            "allow_execute",
            "run_kind",
            "testbenches",
        ):
            self.assertNotIn(key, captured["payload"])

    def test_top_level_live_override_cannot_reuse_dry_run_preflight(self):
        with tempfile.TemporaryDirectory() as workspace:
            service = ToolService(
                workspace=workspace,
                config={
                    "execution": {
                        "dry_run": True,
                        "allow_execute": False,
                        "background": False,
                    }
                },
            )
            with mock.patch.object(
                DualTBOrchestrator,
                "run",
                side_effect=AssertionError(
                    "orchestrator must not run after failed live preflight"
                ),
            ):
                with self.assertRaises(PreflightError):
                    service.start_run(
                        {
                            "dry_run": False,
                            "allow_execute": True,
                            "background": False,
                        }
                    )

    def test_persisted_result_hash_mismatch_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = write_canonical_bundle(
                os.path.join(root, "synthetic_canonical"), signoff=True
            )
            workspace = os.path.join(root, "workspace")
            service = ToolService(workspace=workspace)
            imported = service.import_as_job({"path": fixture})
            job_id = imported["job_id"]
            result_path = os.path.join(
                workspace, "jobs", job_id, "result.json"
            )
            with io.open(result_path, "r", encoding="utf-8") as stream:
                result = json.load(stream)
            result["tampered_after_persist"] = True
            with io.open(result_path, "w", encoding="utf-8") as stream:
                json.dump(result, stream)

            reloaded = ToolService(workspace=workspace)
            job = reloaded.get_job(job_id)

        self.assertEqual("failed", job["status"])
        self.assertFalse(job["result_available"])
        self.assertEqual("ISF-E3002", job["error"]["code"])

    def test_legacy_persisted_signoff_without_hash_is_demoted(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = write_canonical_bundle(
                os.path.join(root, "synthetic_canonical"), signoff=True
            )
            workspace = os.path.join(root, "workspace")
            service = ToolService(workspace=workspace)
            imported = service.import_as_job({"path": fixture})
            job_id = imported["job_id"]
            directory = os.path.join(workspace, "jobs", job_id)
            result_path = os.path.join(directory, "result.json")
            job_path = os.path.join(directory, "job.json")
            with io.open(result_path, "r", encoding="utf-8") as stream:
                result = json.load(stream)
            result["run_kind"] = "signoff"
            result["signoff"] = True
            with io.open(result_path, "w", encoding="utf-8") as stream:
                json.dump(result, stream)
            with io.open(job_path, "r", encoding="utf-8") as stream:
                job = json.load(stream)
            job.pop("result_sha256", None)
            with io.open(job_path, "w", encoding="utf-8") as stream:
                json.dump(job, stream)

            reloaded = ToolService(workspace=workspace)
            loaded = reloaded.get_result(job_id)

        self.assertEqual("replay", loaded["run_kind"])
        self.assertFalse(loaded["signoff"])
        self.assertIsNone(loaded["provenance"])
        self.assertEqual("ISF-E3002", loaded["trust_warning"]["code"])


class RunStateReliabilityTests(unittest.TestCase):
    def test_runtime_file_change_invalidates_saved_identity(self):
        with tempfile.TemporaryDirectory() as root:
            runner = os.path.join(root, "runner.py")
            with io.open(runner, "w", encoding="utf-8") as stream:
                stream.write("# first version\n")
            identity = {
                "files": {
                    "runner": {
                        "path": runner,
                        "sha256": service_module._file_sha256(runner),
                    }
                }
            }
            self.assertEqual(
                {}, service_module._runtime_identity_errors(identity)
            )
            with io.open(runner, "w", encoding="utf-8") as stream:
                stream.write("# changed after checkpoint\n")
            errors = service_module._runtime_identity_errors(identity)
        self.assertIn("runner", errors)
        self.assertNotEqual(
            errors["runner"]["expected"], errors["runner"]["actual"]
        )

    def test_failed_checkpoint_write_preserves_previous_atomic_state(self):
        with tempfile.TemporaryDirectory() as root:
            state = RunState(
                root,
                "job-atomic",
                {"analysis": {"max_harmonic": 3}},
                {"point": "nominal"},
            )
            with open(state.path, "rb") as stream:
                original = stream.read()

            def fail_mid_write(_value, stream, **_kwargs):
                stream.write('{"partial":')
                stream.flush()
                raise RuntimeError("synthetic interrupted write")

            with mock.patch.object(
                run_state_module.json, "dump", side_effect=fail_mid_write
            ):
                with self.assertRaises(RuntimeError):
                    state.mark("vco", {"result": "should-not-commit"})

            with open(state.path, "rb") as stream:
                self.assertEqual(original, stream.read())
            self.assertFalse(
                [
                    name
                    for name in os.listdir(root)
                    if name.startswith(".%s." % STATE_FILENAME)
                    and name.endswith(".tmp")
                ]
            )

            recovered = RunState(
                root,
                "job-atomic",
                {"analysis": {"max_harmonic": 3}},
                {"point": "nominal"},
                resume=True,
            )
            self.assertFalse(recovered.completed("vco"))

    def test_resume_restores_checkpoint_and_contract_mismatch_is_e5002(self):
        config = {"analysis": {"max_harmonic": 3}}
        payload = {"point": "nominal", "resume": False}
        with tempfile.TemporaryDirectory() as root:
            state = RunState(root, "job-resume", config, payload)
            state.mark("collect", {"bundle_path": "/tmp/canonical"})
            state.finish()

            resumed = RunState(
                root,
                "job-resume",
                config,
                dict(payload, resume=True),
                resume=True,
            )
            self.assertTrue(resumed.completed("collect"))
            self.assertEqual(
                {"bundle_path": "/tmp/canonical"}, resumed.result("collect")
            )
            self.assertEqual("succeeded", resumed.value["status"])

            with self.assertRaises(ResumeError) as caught:
                RunState(
                    root,
                    "job-resume",
                    {"analysis": {"max_harmonic": 5}},
                    payload,
                    resume=True,
                )
            self.assertEqual("ISF-E5002", caught.exception.code)
            self.assertNotEqual(
                caught.exception.details["existing"],
                caught.exception.details["requested"],
            )


class ProtocolAdapterReliabilityTests(unittest.TestCase):
    @staticmethod
    def _config():
        return {
            "submit_command": ["submit", "--output", "{output_dir}"],
            "poll_command": ["poll", "{scheduler_job_id}"],
            "collect_command": [
                "collect",
                "{scheduler_job_id}",
                "{output_dir}",
            ],
            "verify_paths": ["{output_dir}/artifact.txt"],
            "job_id_regex": r"JOB=(?P<job_id>[A-Za-z0-9-]+)",
            "poll_interval_s": 0.01,
            "timeout": 2.0,
            "inherit_env": False,
        }

    def test_fake_runner_submit_poll_collect_and_verify_success(self):
        with tempfile.TemporaryDirectory() as root:
            artifact = os.path.join(root, "artifact.txt")
            calls = []

            def fake_runner(command, cwd, environment, timeout):
                calls.append(
                    {
                        "command": list(command),
                        "cwd": cwd,
                        "environment": dict(environment),
                        "timeout": timeout,
                    }
                )
                if command[0] == "submit":
                    return {
                        "returncode": 0,
                        "stdout": "JOB=scheduler-42\n",
                        "stderr": "",
                    }
                if command[0] == "poll":
                    return {
                        "returncode": 0,
                        "stdout": "COMPLETED\n",
                        "stderr": "",
                    }
                if command[0] == "collect":
                    with io.open(artifact, "w", encoding="utf-8") as stream:
                        stream.write("collected\n")
                    return {
                        "returncode": 0,
                        "stdout": "collected\n",
                        "stderr": "",
                    }
                raise AssertionError("unexpected command: %r" % (command,))

            result = ProtocolAdapter(
                self._config(), runner=fake_runner
            ).submit({"output_dir": root}, dry_run=False)

            self.assertEqual("succeeded", result["status"])
            self.assertFalse(result["dry_run"])
            self.assertEqual("collected\n", result["stdout"])
            self.assertEqual(
                [
                    ["submit", "--output", root],
                    ["poll", "scheduler-42"],
                    ["collect", "scheduler-42", root],
                ],
                [call["command"] for call in calls],
            )
            self.assertTrue(all(call["environment"] == {} for call in calls))
            self.assertEqual(
                "scheduler-42", result["metadata"]["scheduler_job_id"]
            )
            self.assertEqual(
                [os.path.realpath(artifact)],
                result["metadata"]["verified_paths"],
            )
            self.assertTrue(os.path.isfile(artifact))

    def test_fake_runner_missing_job_id_is_e4101(self):
        calls = []

        def fake_runner(command, cwd, environment, timeout):
            calls.append(list(command))
            return {
                "returncode": 0,
                "stdout": "submission accepted\n",
                "stderr": "",
            }

        with tempfile.TemporaryDirectory() as root:
            adapter = ProtocolAdapter(self._config(), runner=fake_runner)
            with self.assertRaises(AdapterExecutionError) as caught:
                adapter.submit({"output_dir": root}, dry_run=False)

        self.assertEqual("ISF-E4101", caught.exception.code)
        self.assertIn("did not contain a job id", caught.exception.message)
        self.assertEqual([["submit", "--output", root]], calls)

    def test_scheduler_id_is_checkpointable_and_resume_does_not_resubmit(self):
        progress = []
        first_calls = []

        def interrupted_runner(command, cwd, environment, timeout):
            first_calls.append(list(command))
            if command[0] == "submit":
                return {
                    "returncode": 0,
                    "stdout": "JOB=scheduler-77\n",
                    "stderr": "",
                }
            raise AdapterExecutionError("synthetic process interruption")

        with tempfile.TemporaryDirectory() as root:
            adapter = ProtocolAdapter(self._config(), runner=interrupted_runner)
            with self.assertRaises(AdapterExecutionError):
                adapter.submit(
                    {
                        "output_dir": root,
                        "submission_key": "job:vco",
                    },
                    dry_run=False,
                    on_progress=lambda value: progress.append(value),
                )
            self.assertEqual(
                [["submit", "--output", root], ["poll", "scheduler-77"]],
                first_calls,
            )
            self.assertEqual("scheduler-77", progress[0]["scheduler_job_id"])
            self.assertEqual("job:vco", progress[0]["submission_key"])

            resumed_calls = []
            artifact = os.path.join(root, "artifact.txt")

            def resumed_runner(command, cwd, environment, timeout):
                resumed_calls.append(list(command))
                if command[0] == "poll":
                    return {
                        "returncode": 0,
                        "stdout": "COMPLETED\n",
                        "stderr": "",
                    }
                if command[0] == "collect":
                    with io.open(artifact, "w", encoding="utf-8") as stream:
                        stream.write("recovered\n")
                    return {
                        "returncode": 0,
                        "stdout": "recovered\n",
                        "stderr": "",
                    }
                raise AssertionError("resume must not submit again")

            result = ProtocolAdapter(
                self._config(), runner=resumed_runner
            ).submit(
                {
                    "output_dir": root,
                    "submission_key": "job:vco",
                },
                dry_run=False,
                resume=progress[0],
            )

        self.assertEqual(
            [["poll", "scheduler-77"], ["collect", "scheduler-77", root]],
            resumed_calls,
        )
        self.assertTrue(result["metadata"]["resumed_scheduler_job"])
        self.assertEqual("scheduler-77", result["metadata"]["scheduler_job_id"])


class OrchestratorResumeReliabilityTests(unittest.TestCase):
    def test_string_execution_consent_cannot_start_external_runner(self):
        runner = mock.Mock(
            side_effect=AssertionError("external runner must not be called")
        )
        orchestrator = DualTBOrchestrator(
            {
                "execution": {
                    "dry_run": False,
                    "allow_execute": "false",
                }
            },
            command_runner=runner,
        )
        with self.assertRaisesRegex(
            ValidationError, "execution.allow_execute must be a boolean"
        ):
            orchestrator.run("strict-consent", {})
        runner.assert_not_called()

    def test_string_resume_flag_cannot_enable_resume(self):
        runner = mock.Mock(
            side_effect=AssertionError("external runner must not be called")
        )
        orchestrator = DualTBOrchestrator(
            {
                "execution": {
                    "dry_run": True,
                    "allow_execute": False,
                    "resume_enabled": "false",
                }
            },
            command_runner=runner,
        )
        with self.assertRaisesRegex(
            ValidationError, "execution.resume_enabled must be a boolean"
        ):
            orchestrator.run("strict-resume", {"resume": True})
        runner.assert_not_called()

    def test_string_resume_request_is_rejected(self):
        runner = mock.Mock(
            side_effect=AssertionError("external runner must not be called")
        )
        orchestrator = DualTBOrchestrator(
            {
                "execution": {
                    "dry_run": True,
                    "allow_execute": False,
                    "resume_enabled": True,
                }
            },
            command_runner=runner,
        )
        with self.assertRaisesRegex(ValidationError, "resume must be a boolean"):
            orchestrator.run("strict-resume-request", {"resume": "false"})
        runner.assert_not_called()

    def test_resume_skips_only_successful_external_stages(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []
            replica_attempts = [0]

            def runner(command, cwd, environment, timeout):
                text = command[-1]
                calls.append(text)
                if "replica" in text:
                    replica_attempts[0] += 1
                    if replica_attempts[0] == 1:
                        return {"returncode": 7, "stdout": "", "stderr": "synthetic"}
                return {"returncode": 0, "stdout": "ok", "stderr": ""}

            config = {
                "execution": {
                    "dry_run": False,
                    "allow_execute": True,
                    "resume_enabled": True,
                },
                "auto": {
                    "submitter": "custom_shell",
                    "vco_command": "stage vco",
                    "exchange_command": "stage exchange",
                    "replica_command": "stage replica",
                    "extract_command": "stage extract",
                    "timeout_s": 10,
                },
            }
            payload = {
                "dry_run": False,
                "allow_execute": True,
                "run_kind": "replay",
                "run_root": os.path.join(root, "run"),
                "bundle_path": os.path.join(root, "canonical"),
            }
            orchestrator = DualTBOrchestrator(config, command_runner=runner)
            with self.assertRaises(AdapterExecutionError) as first:
                orchestrator.run("resume-job", dict(payload))
            self.assertEqual("ISF-E4005", first.exception.code)
            self.assertEqual(
                ["stage vco", "stage exchange", "stage replica"], calls
            )

            calls[:] = []
            with self.assertRaises(AdapterExecutionError) as second:
                orchestrator.run("resume-job", dict(payload, resume=True))
            self.assertEqual("ISF-E3001", second.exception.code)
            self.assertEqual(["stage replica", "stage extract"], calls)

            checkpoint = RunState(
                payload["run_root"],
                "resume-job",
                config,
                dict(payload, resume=True),
                resume=True,
            )
            for stage in ("submit_vco", "exchange", "submit_replica", "extract"):
                self.assertTrue(checkpoint.completed(stage), stage)


if __name__ == "__main__":
    unittest.main()
