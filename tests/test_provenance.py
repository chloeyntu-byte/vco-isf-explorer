"""Fail-closed checks for formal automatic-run provenance."""

from __future__ import unicode_literals

import copy
import hashlib
import io
import json
import os
import tempfile
import time
import unittest

from isf_tool.core.errors import AdapterExecutionError
from isf_tool.core.orchestrator import (
    _tree_identity,
    _validate_bundle_provenance,
)
from isf_tool.core.physical_profile import (
    LEGACY_PHYSICAL_PROFILE,
    resolve_physical_profile,
)

try:
    from .synthetic_fixture import make_canonical, write_canonical_bundle
except ImportError:  # unittest discovery loads test modules as top-level names
    from synthetic_fixture import make_canonical, write_canonical_bundle



def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        value.update(stream.read())
    return value.hexdigest()


def build_bundle(root, point_count=6):
    bundle = os.path.join(root, "canonical")
    simulation = os.path.join(root, "simulation")
    canonical = make_canonical(signoff=True, point_count=point_count)
    write_canonical_bundle(bundle, canonical=canonical)
    os.makedirs(simulation)
    points = []
    for row in canonical["vco_results"]:
        if row["point"] not in points:
            points.append(row["point"])

    now = time.time()
    records = []
    for point in points:
        for role in ("vco", "replica"):
            psf = os.path.join(simulation, point, role, "psf")
            os.makedirs(psf)
            netlist_dir = os.path.join(simulation, point, role, "netlist")
            os.makedirs(netlist_dir)
            netlist = os.path.join(netlist_dir, "input.scs")
            sim_done = os.path.join(psf, ".simDone")
            spectre_out = os.path.join(psf, "spectre.out")
            with io.open(netlist, "w", encoding="utf-8") as stream:
                stream.write("// owned unit-test netlist\n")
            with io.open(sim_done, "w", encoding="utf-8") as stream:
                stream.write("owned test run\n")
            with io.open(spectre_out, "w", encoding="utf-8") as stream:
                stream.write("spectre completes with 0 errors, 0 warnings, and 0 notices.\n")
            psf_identity = _tree_identity(psf)
            records.append(
                {
                    "point": point,
                    "role": role,
                    "psf_dir": psf,
                    "sim_done_mtime": os.path.getmtime(sim_done),
                    "spectre_out_mtime": os.path.getmtime(spectre_out),
                    "spectre_out_sha256": digest(spectre_out),
                    "netlist_path": netlist,
                    "netlist_sha256": digest(netlist),
                    "psf_tree_sha256": psf_identity["sha256"],
                    "psf_file_count": psf_identity["files"],
                    "psf_bytes": psf_identity["bytes"],
                    "zero_errors": True,
                }
            )
    engine_path = os.path.join(root, "engine.py")
    orchestrator_path = os.path.join(root, "orchestrator.py")
    config_path = os.path.join(root, "engine.json")
    with io.open(engine_path, "w", encoding="utf-8") as stream:
        stream.write("# owned unit-test engine\n")
    with io.open(orchestrator_path, "w", encoding="utf-8") as stream:
        stream.write("# owned unit-test orchestrator\n")
    engine_config = {
        "physical_profile": copy.deepcopy(LEGACY_PHYSICAL_PROFILE)
    }
    with io.open(config_path, "w", encoding="utf-8") as stream:
        json.dump(engine_config, stream)
    profile_info = resolve_physical_profile(
        engine_config, config_path=config_path, allow_legacy=False
    )
    runtime_identity = {}
    for label, path in (
        ("engine_config", config_path),
        ("runner", engine_path),
        ("engine", engine_path),
        ("exporter", engine_path),
        ("calculation", engine_path),
        ("dual_tb", engine_path),
        ("physical_profile_module", engine_path),
        ("source_paths_module", engine_path),
        ("orchestrator", orchestrator_path),
        ("model", config_path),
        ("spectre", engine_path),
        ("psf", engine_path),
    ):
        runtime_identity[label] = {
            "path": path,
            "sha256": digest(path),
        }
    metadata = copy.deepcopy(canonical["metadata"])
    metadata.update(
        {
            "run_kind": "signoff",
            "signoff": True,
            "job_id": "unit-job",
            "generated_at_epoch": now,
            "source_run_root": simulation,
            "engine": engine_path,
            "config": config_path,
            "engine_sha256": digest(engine_path),
            "config_sha256": digest(config_path),
            "physical_profile_id": profile_info["profile"]["profile_id"],
            "physical_profile_sha256": profile_info["sha256"],
            "physical_profile_explicit": True,
            "runtime_identity": runtime_identity,
            "psf_runs": records,
        }
    )
    metadata_path = os.path.join(bundle, "metadata.json")
    with io.open(metadata_path, "w", encoding="utf-8") as stream:
        json.dump(metadata, stream)
    manifest_files = {}
    for name in ("vco_results.csv", "replica_results.csv", "workpoint.csv", "metadata.json"):
        path = os.path.join(bundle, name)
        manifest_files[name] = {"sha256": digest(path), "bytes": os.path.getsize(path)}
    with io.open(os.path.join(bundle, "manifest.json"), "w", encoding="utf-8") as stream:
        json.dump({"job_id": "unit-job", "files": manifest_files}, stream)
    return bundle, simulation, records, now


class ProvenanceTests(unittest.TestCase):
    def test_validator_rechecks_real_log_hash_and_content(self):
        with tempfile.TemporaryDirectory() as root:
            bundle, simulation, records, now = build_bundle(root)
            result = _validate_bundle_provenance(
                {"path": bundle}, "unit-job", now - 1.0, simulation, "signoff"
            )
            self.assertEqual(12, result["psf_runs"])
            self.assertEqual(5, len(result["files"]))
            log = os.path.join(records[0]["psf_dir"], "spectre.out")
            with io.open(log, "w", encoding="utf-8") as stream:
                stream.write("fatal: fabricated metadata must not pass\n")
            with self.assertRaises(AdapterExecutionError):
                _validate_bundle_provenance(
                    {"path": bundle}, "unit-job", now - 1.0, simulation, "signoff"
                )

    def test_validator_rechecks_complete_psf_tree_identity(self):
        with tempfile.TemporaryDirectory() as root:
            bundle, simulation, records, now = build_bundle(root)
            _validate_bundle_provenance(
                {"path": bundle},
                "unit-job",
                now - 1.0,
                simulation,
                "signoff",
            )
            unexpected = os.path.join(
                records[0]["psf_dir"], "changed-result.raw"
            )
            with io.open(unexpected, "w", encoding="utf-8") as stream:
                stream.write("changed after export\n")
            with self.assertRaises(AdapterExecutionError):
                _validate_bundle_provenance(
                    {"path": bundle},
                    "unit-job",
                    now - 1.0,
                    simulation,
                    "signoff",
                )

    def test_formal_signoff_rejects_one_point_smoke(self):
        with tempfile.TemporaryDirectory() as root:
            bundle, simulation, _records, now = build_bundle(root, point_count=1)
            with self.assertRaises(AdapterExecutionError) as raised:
                _validate_bundle_provenance(
                    {"path": bundle}, "unit-job", now - 1.0, simulation, "signoff"
                )
            self.assertIn("at least 6 design points", str(raised.exception))

    def test_bundled_signoff_consumes_every_preflight_runtime_identity(self):
        with tempfile.TemporaryDirectory() as root:
            bundle, simulation, _records, now = build_bundle(root)
            metadata_path = os.path.join(bundle, "metadata.json")
            manifest_path = os.path.join(bundle, "manifest.json")
            with io.open(metadata_path, "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            source_netlist = os.path.join(root, "source-input.scs")
            with io.open(source_netlist, "w", encoding="utf-8") as stream:
                stream.write("// preflight source identity\n")
            source_record = {
                "path": source_netlist,
                "sha256": digest(source_netlist),
            }
            metadata["runtime_identity"]["source_netlist_000"] = dict(
                source_record
            )
            with io.open(metadata_path, "w", encoding="utf-8") as stream:
                json.dump(metadata, stream)
            with io.open(manifest_path, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            manifest["files"]["metadata.json"] = {
                "sha256": digest(metadata_path),
                "bytes": os.path.getsize(metadata_path),
            }
            with io.open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream)
            run_config = {
                "execution": {"adapter_mode": "bundled"},
                "runtime_identity": {
                    "engine_config_path": metadata["config"],
                    "engine_config_sha256": metadata["config_sha256"],
                    "engine_sha256": metadata["engine_sha256"],
                    "files": copy.deepcopy(metadata["runtime_identity"]),
                },
            }
            _validate_bundle_provenance(
                {"path": bundle},
                "unit-job",
                now - 1.0,
                simulation,
                "signoff",
                run_config,
            )

            with io.open(source_netlist, "w", encoding="utf-8") as stream:
                stream.write("// replaced after preflight\n")
            metadata["runtime_identity"]["source_netlist_000"][
                "sha256"
            ] = digest(source_netlist)
            with io.open(metadata_path, "w", encoding="utf-8") as stream:
                json.dump(metadata, stream)
            with io.open(manifest_path, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            manifest["files"]["metadata.json"] = {
                "sha256": digest(metadata_path),
                "bytes": os.path.getsize(metadata_path),
            }
            with io.open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream)

            with self.assertRaises(AdapterExecutionError):
                _validate_bundle_provenance(
                    {"path": bundle},
                    "unit-job",
                    now - 1.0,
                    simulation,
                    "signoff",
                    run_config,
                )

    def test_bundled_signoff_rejects_orchestrator_changed_after_preflight(self):
        with tempfile.TemporaryDirectory() as root:
            bundle, simulation, _records, now = build_bundle(root)
            metadata_path = os.path.join(bundle, "metadata.json")
            manifest_path = os.path.join(bundle, "manifest.json")
            with io.open(metadata_path, "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            run_config = {
                "execution": {"adapter_mode": "bundled"},
                "runtime_identity": {
                    "engine_config_path": metadata["config"],
                    "engine_config_sha256": metadata["config_sha256"],
                    "engine_sha256": metadata["engine_sha256"],
                    "files": copy.deepcopy(metadata["runtime_identity"]),
                },
            }
            _validate_bundle_provenance(
                {"path": bundle},
                "unit-job",
                now - 1.0,
                simulation,
                "signoff",
                run_config,
            )

            orchestrator_path = metadata["runtime_identity"]["orchestrator"][
                "path"
            ]
            with io.open(orchestrator_path, "w", encoding="utf-8") as stream:
                stream.write("# replaced after preflight\n")
            metadata["runtime_identity"]["orchestrator"]["sha256"] = digest(
                orchestrator_path
            )
            with io.open(metadata_path, "w", encoding="utf-8") as stream:
                json.dump(metadata, stream)
            with io.open(manifest_path, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            manifest["files"]["metadata.json"] = {
                "sha256": digest(metadata_path),
                "bytes": os.path.getsize(metadata_path),
            }
            with io.open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream)

            with self.assertRaises(AdapterExecutionError):
                _validate_bundle_provenance(
                    {"path": bundle},
                    "unit-job",
                    now - 1.0,
                    simulation,
                    "signoff",
                    run_config,
                )

    def test_maestro_pair_signoff_distinguishes_template_from_materialized_config(self):
        with tempfile.TemporaryDirectory() as root:
            bundle, simulation, _records, now = build_bundle(root)
            metadata_path = os.path.join(bundle, "metadata.json")
            manifest_path = os.path.join(bundle, "manifest.json")
            with io.open(metadata_path, "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            template = os.path.join(root, "engine-template.json")
            pair_manifest = os.path.join(root, "pair-source-manifest.json")
            private_flicker = os.path.join(root, "private-flicker.scs")
            for path, text in (
                (template, "{}\n"),
                (pair_manifest, "{}\n"),
                (private_flicker, "// private flicker template\n"),
            ):
                with io.open(path, "w", encoding="utf-8") as stream:
                    stream.write(text)
            metadata["runtime_identity"].update(
                {
                    "engine_template": {
                        "path": template,
                        "sha256": digest(template),
                    },
                    "maestro_source_manifest": {
                        "path": pair_manifest,
                        "sha256": digest(pair_manifest),
                    },
                    "private_flicker_template": {
                        "path": private_flicker,
                        "sha256": digest(private_flicker),
                    },
                }
            )
            bindings = {
                role: {
                    "maestro_dir": os.path.join(root, role),
                    "test": role + "_test",
                    "binding_fingerprint": role + "-tb-fp",
                    "history": os.path.join(root, role + ".rdb"),
                    "history_fingerprint": role + "-history-fp",
                }
                for role in ("vco", "flicker")
            }
            metadata["maestro_pair_binding"] = copy.deepcopy(bindings)
            with io.open(metadata_path, "w", encoding="utf-8") as stream:
                json.dump(metadata, stream)
            with io.open(manifest_path, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            manifest["files"]["metadata.json"] = {
                "sha256": digest(metadata_path),
                "bytes": os.path.getsize(metadata_path),
            }
            with io.open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream)

            expected_files = copy.deepcopy(metadata["runtime_identity"])
            expected_files["engine_config"] = {
                "path": template,
                "sha256": digest(template),
            }
            run_config = {
                "source_mode": "maestro_pair",
                "execution": {"adapter_mode": "bundled"},
                "runtime_identity": {
                    "engine_config_path": template,
                    "engine_config_sha256": digest(template),
                    "engine_sha256": metadata["engine_sha256"],
                    "files": expected_files,
                    "maestro_pair": {
                        "engine_template_path": template,
                        "engine_template_sha256": digest(template),
                        "bindings": copy.deepcopy(bindings),
                    },
                },
            }
            _validate_bundle_provenance(
                {"path": bundle},
                "unit-job",
                now - 1.0,
                simulation,
                "signoff",
                run_config,
            )

            metadata["maestro_pair_binding"]["flicker"][
                "history_fingerprint"
            ] = "changed"
            with io.open(metadata_path, "w", encoding="utf-8") as stream:
                json.dump(metadata, stream)
            with io.open(manifest_path, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            manifest["files"]["metadata.json"] = {
                "sha256": digest(metadata_path),
                "bytes": os.path.getsize(metadata_path),
            }
            with io.open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream)
            with self.assertRaises(AdapterExecutionError):
                _validate_bundle_provenance(
                    {"path": bundle},
                    "unit-job",
                    now - 1.0,
                    simulation,
                    "signoff",
                    run_config,
                )


if __name__ == "__main__":
    unittest.main()
