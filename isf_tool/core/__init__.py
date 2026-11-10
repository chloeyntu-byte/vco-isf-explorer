"""Public API for the portable ISF backend core."""

from .adapters import (
    CustomShellAdapter,
    ProtocolAdapter,
    RunSimulationAdapter,
    ShellAdapter,
    TEMPLATE_VARIABLE_WHITELIST,
    adapter_from_config,
    render_template,
)
from .bundle import CSVBundleImporter, import_csv_bundle
from .discovery import discover_result_directories, inspect_rdb
from .dual_tb import DualTBImporter, import_dual_tb_bundle, recompute_dual_tb
from .events import EventRecorder
from .error_catalog import error_entry, load_error_catalog
from .models import POINT_TABLES, empty_result, normalize_result
from .maestro_pair import (
    discover_maestro_setups,
    inspect_maestro,
    inspect_maestro_pair,
    normalize_maestro_dir,
)
from .orchestrator import AutomaticDualTBOrchestrator, DualTBOrchestrator
from .physical_profile import (
    inspect_engine_contract,
    load_engine_config,
    resolve_physical_profile,
)
from .preflight import detect_environment, detect_inputs, detect_pdk, preflight, resolve_config
from .run_state import RunState, contract_sha256
from .psf_extract import (
    PsfReader,
    hbxf_sideband_mapping,
    load_maestro_runs,
    pair_runs,
)
from .service import ToolService

__all__ = [
    "AutomaticDualTBOrchestrator",
    "CSVBundleImporter",
    "CustomShellAdapter",
    "DualTBImporter",
    "DualTBOrchestrator",
    "EventRecorder",
    "POINT_TABLES",
    "PsfReader",
    "ProtocolAdapter",
    "RunSimulationAdapter",
    "ShellAdapter",
    "TEMPLATE_VARIABLE_WHITELIST",
    "ToolService",
    "adapter_from_config",
    "contract_sha256",
    "detect_environment",
    "detect_inputs",
    "detect_pdk",
    "discover_maestro_setups",
    "discover_result_directories",
    "empty_result",
    "import_csv_bundle",
    "import_dual_tb_bundle",
    "hbxf_sideband_mapping",
    "inspect_rdb",
    "inspect_maestro",
    "inspect_maestro_pair",
    "inspect_engine_contract",
    "load_maestro_runs",
    "load_error_catalog",
    "load_engine_config",
    "normalize_result",
    "normalize_maestro_dir",
    "recompute_dual_tb",
    "render_template",
    "pair_runs",
    "preflight",
    "resolve_config",
    "resolve_physical_profile",
    "RunState",
    "error_entry",
]
