"""Command line routing shared by source and installed launches."""

import argparse
import json
import os
import sys
from pathlib import Path


def _diagnose(arguments):
    from .core.environment_report import format_environment_report, inspect_environment
    from .core.errors import ValidationError

    parser = argparse.ArgumentParser(description="VCO ISF Explorer read-only environment diagnostics")
    parser.add_argument("--diagnose", action="store_true", help="inspect the local environment")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--config", type=Path, help="explicit application config JSON")
    parser.add_argument("--maestro-context-file", type=Path)
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    args = parser.parse_args(arguments)
    workspace = (args.workspace or Path(os.environ.get("ISF_WORKSPACE", Path.home() / ".vco_isf_explorer"))).expanduser().resolve()
    config_path = args.config or workspace / "config.json"
    try:
        config = json.loads(config_path.expanduser().read_text(encoding="utf-8")) if args.config or config_path.is_file() else {}
        if not isinstance(config, dict):
            raise ValueError("application config must be a JSON object")
        context = None
        if args.maestro_context_file:
            from .core.maestro_context import load_maestro_context
            context = load_maestro_context(args.maestro_context_file, consume=False)
        report = inspect_environment(config, workspace, context)
    except (OSError, ValueError, ValidationError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_environment_report(report))
    return 0 if report["status"] == "ready" else 1


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--diagnose" in arguments:
        return _diagnose(arguments)
    if "--web" in arguments:
        arguments.remove("--web")
        from .server import main as web_main
        return web_main(arguments)
    from .tk.app import main as tk_main
    return tk_main(arguments)
