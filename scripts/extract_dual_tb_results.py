#!/usr/bin/env python3
"""Inspect and pair two Maestro histories without modifying simulation data."""

from __future__ import annotations

import argparse
import json
import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOTSTRAP_ROOTS = [PROJECT_ROOT]
if os.path.basename(PROJECT_ROOT) == "_runtime" and os.path.basename(
    os.path.dirname(PROJECT_ROOT)
) == "isf_tool":
    BOOTSTRAP_ROOTS.append(os.path.dirname(os.path.dirname(PROJECT_ROOT)))
for bootstrap_root in reversed(BOOTSTRAP_ROOTS):
    if bootstrap_root not in sys.path:
        sys.path.insert(0, bootstrap_root)

from isf_tool.core.errors import CoreError  # noqa: E402
from isf_tool.core.psf_extract import (  # noqa: E402
    hbxf_sideband_mapping,
    hbxf_sideband_suffixes,
    load_maestro_runs,
    pair_runs,
)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Read exact point/test PSF manifests from Maestro RDB files. "
            "This command does not run Spectre or write into result histories."
        )
    )
    parser.add_argument("--inspect-vco-rdb", metavar="PATH")
    parser.add_argument("--inspect-replica-rdb", metavar="PATH")
    parser.add_argument(
        "--test",
        help="exact test name or testID applied to both RDBs unless overridden",
    )
    parser.add_argument("--vco-test", help="VCO test name or testID")
    parser.add_argument("--replica-test", help="replica test name or testID")
    parser.add_argument(
        "--sweep-key",
        action="append",
        default=[],
        help="shared sweep parameter used after corner for one-to-one pairing",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="include runs that lack .simDone or terminal Spectre zero-error proof",
    )
    parser.add_argument(
        "--max-harmonic",
        type=int,
        default=3,
        help="include the signed-k to HBXF suffix map in the manifest (default: 3)",
    )
    parser.add_argument(
        "--manifest",
        default="-",
        metavar="PATH",
        help="JSON output path, or '-' for stdout (default: -)",
    )
    return parser


def build_manifest(args):
    if not args.inspect_vco_rdb and not args.inspect_replica_rdb:
        raise ValueError("at least one --inspect-vco-rdb/--inspect-replica-rdb is required")
    require_complete = not args.allow_incomplete
    manifest = {
        "schema_version": 1,
        "read_only": True,
        "require_complete": require_complete,
        "sweep_keys": list(args.sweep_key),
        "hbxf_sideband_suffix": {
            str(key): value
            for key, value in hbxf_sideband_mapping(args.max_harmonic).items()
        },
        "hbxf_sideband_suffixes": hbxf_sideband_suffixes(args.max_harmonic),
    }
    vco_runs = None
    replica_runs = None
    if args.inspect_vco_rdb:
        vco_runs = load_maestro_runs(
            args.inspect_vco_rdb,
            test=args.vco_test or args.test,
            require_complete=require_complete,
        )
        manifest["vco"] = {
            "rdb": os.path.realpath(args.inspect_vco_rdb),
            "test": args.vco_test or args.test,
            "runs": vco_runs,
        }
    if args.inspect_replica_rdb:
        replica_runs = load_maestro_runs(
            args.inspect_replica_rdb,
            test=args.replica_test or args.test,
            require_complete=require_complete,
        )
        manifest["replica"] = {
            "rdb": os.path.realpath(args.inspect_replica_rdb),
            "test": args.replica_test or args.test,
            "runs": replica_runs,
        }
    if vco_runs is not None and replica_runs is not None:
        manifest["pairs"] = pair_runs(vco_runs, replica_runs, args.sweep_key)
    return manifest


def _write_manifest(value, destination):
    text = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if destination == "-":
        sys.stdout.write(text)
        return
    path = os.path.realpath(os.path.abspath(os.path.expanduser(destination)))
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = "%s.tmp-%d" % (path, os.getpid())
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(args)
        _write_manifest(manifest, args.manifest)
    except (CoreError, OSError, ValueError) as exc:
        if isinstance(exc, CoreError):
            payload = exc.to_dict()
        else:
            payload = {"code": "manifest_error", "message": str(exc), "details": {}}
        sys.stderr.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
