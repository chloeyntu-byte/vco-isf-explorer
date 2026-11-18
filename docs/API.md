# ISF integration API 1.0.0

The three adapters return `isf.prepare-response.v1` against the published
contract in `isf_tool/contracts/isf-contract.schema.json`. Every public response
carries `contract_version=1.0.0` alongside its versioned `schema`; the spec also
retains `extensions.contract_version`. The response schema accepts older saved
envelopes without the explicit field. Reading or retrying an older job adds the
field to the returned object without rewriting its saved evidence. `complete` freezes
one plan and invokes only TB Generator's plan/materialize/run/results functions
for TB/config/Maestro. The previous desktop, `--web`, and `--diagnose` routes remain.

```python
from isf_tool import api
proposal = api.prepare_from_design_lab(generated_dir, workspace=work,
    dut={"lib": "PRIVATE_DUT", "cell": "VCO", "view": "schematic"},
    cds_lib=cds, library_path=library,
    maestro={"lib": "PRIVATE_TB", "cell": "TB", "view": "maestro", "test": "HB_ISF"},
    tbgen_root=tbgen_checkout,
    overrides={"gm": {"target_instance": "DUT.MN1"}})
proposal = api.complete(proposal['spec'], overrides=decisions)
job = api.run(proposal['spec_path'], spec_hash=proposal['spec_hash'], workspace=work,
              request_id='caller-unique-id', wait=False)
job = api.status(job['job_id'], workspace=work)
result = api.results(job['job_id'], workspace=work)
api.open_ui(proposal['spec_path'], workspace=work)  # no auto-run
```

A consumes an already generated Design Lab directory and exported DUT. Generate and export the DUT in the source application first. B's request has `cds_lib`, `library_path`, `dut`,
and `maestro` (`lib/cell/view/test/source_test`); the specified original setup is
augmented. C uses `dut`, `cds_lib`, `maestro`, and explicit `ports/supplies/controls/
registers/variables/models/initial_conditions`. Both may pass a `topology_file`
for the hash-bound component selector. `tbgen_spec` is also accepted as a complete
native handoff, with ISF-only settings in overrides. `browse_libraries` and
`describe_cell` delegate actual OA reads and return classifications to confirm.

Override fields are nested objects or JSON Pointers. Missing fguess, gm, output,
driver, or unresolved TB Generator ask blocks readiness. Supply and bias values,
register LSB ordering/domain/code must be explicit; unknown inputs are not grounded.
The component selector proves the gm occurrence and D/G/S/B from a supplied netlist. When
only an OA cell is supplied, gm path is explicit and final binding is qualified
against the newly completed native history by the Maestro replay path.

B always returns a write confirmation ask. Confirm after reviewing its plan:

```python
from isf_tool.core.testbench import digest
proposal = api.complete(proposal['spec'], confirmations=[
    {'field': '/execution/materialization', 'value_hash': digest('source_new_test')}])
```

The stored consent binds source hashes, source/new test identity, analyses and saves;
changing the proposal requires confirmation again. TB Generator backs up the actual
setup, refuses locks/open sessions and verifies original tests/global settings and
native getter snapshots. B inherits enabled HB without setting it again; disabled
HB does not supply fguess and is completed using an explicit user/known frequency. Original variables/corners/
sweeps/models remain native; ISF does not reconstruct them. It is currently limited
to the qualified IC618 v6/v5 format and TB Generator's supported sweep semantics.

`materialize(spec_path, spec_hash=...)` optionally prepares native assets without
running. `run` materializes then runs both stages in an owned detached worker. Its
manifest records `tbgen.materialize/run/results` and `isf.preflight/run`, actual
native receipts, exact history/RDB/points, the ISF job and canonical result. This
release uses the documented transitional path: the native completed history is
then privately rerun by the existing Maestro + generated replica engine. It does
not introduce a numerical implementation. Distinct offsets in ISF are exact.
For one requested offset, native acquisition also samples twice that offset to
retain IC618 HBXF family coordinates; this is visible in the plan and
`extensions.native_offsets_hz`, and does not add an ISF result offset.

A workspace accepts one active API job. Same request_id + spec_hash returns the
existing handle without resubmitting; a changed spec returns SOURCE_CHANGED.
Source setup, provided source files and model hashes are checked before execution.
The richer netlist/model provenance remains enforced by the bundled replay engine.
Prepare readiness is distinct from successful materialization and completed results.

```bash
python -B run.py prepare design-lab --input request.json --workspace W --tbgen-root TBGEN --json
python -B run.py prepare maestro --input request.json --workspace W --tbgen-root TBGEN --json
python -B run.py prepare cell --input request.json --workspace W --tbgen-root TBGEN --json
python -B run.py complete --spec W/preparations/ID/HASH.json --decisions decisions.json --json
python -B run.py materialize --spec SPEC --spec-hash HASH --json
python -B run.py run --spec SPEC --spec-hash HASH --workspace W --request-id ID --wait --json
python -B run.py status --job JOB --workspace W --json
python -B run.py results --job JOB --workspace W --json
python -B run.py ui --spec SPEC --workspace W
python -B run.py browse --cds-lib CDS --workspace W --tbgen-root TBGEN --json
python -B run.py describe-cell --input cell-request.json --workspace W --tbgen-root TBGEN --json
python -B run.py capabilities --tbgen-root TBGEN --json
```

`--json` emits exactly one object, including invalid arguments and failures; progress
and native output go to logs/stderr. Exit codes: 0 accepted/ready/completed, 2 invalid
or incomplete, 3 unsupported capability, 4 simulation/extraction failure, 5 timeout,
6 source/hash/request-id conflict. Accepted background work is not numerical success.
The integration currently supports `run_kind=replay`; signoff/plan are refused.
Generated thermal requires supported Spectre and noise-source evidence.
The symmetric shortcut also requires measured pair-symmetry evidence. Unsupported result filters are refused rather than silently ignored.

## Current Maestro session transaction

TB Generator must provide `plan_session_add`, `materialize_session_add` and
`lc_hb_defaults` (the phase-2 API). `capabilities.gui_add_test.available` detects
the transaction functions. Open ISF from the exact Maestro window's menu to
capture session/window/setup handle, generation, dirty flag and saved revision.
Older contexts remain readable; they cannot execute a session add.

The B review includes the complete saved setup XML (all test/global/corner/sweep
settings), selected source test, each added/overridden analysis, saves, outputs
and the explicit qualification contract. Only hb/hbnoise/hbxf and the ISF nodes
and target drain are required; other inherited analyses/outputs/saves are
preserved. The original HB remains authoritative, including when a saved ISF
request already contains incremental hbnoise/hbxf. An explicitly chosen peer in
`gm.peer_instances` survives the selector and reaches the existing native
master/geometry/bias symmetry proof; selecting a peer is not itself proof.

```python
handle = api.prepare_session_add(proposal['spec_path'],
    spec_hash=proposal['spec_hash'], context=maestro_context,
    save_policy='reject')  # use 'save' only after explicit dirty-state consent
handle = api.submit_session_add(handle)
state = api.session_add_status(handle)  # awaiting_session_confirmation / added /
                                       # rejected / rolled_back / failed
# Only after status=added:
api.accept_session_add(handle)
job = api.run(proposal['spec_path'], spec_hash=proposal['spec_hash'],
    workspace=work, request_id='session-add-unique-id')
```

The private bridge binds the exact invoking session. A guardian acquires the
original library and sim-slot locks before publishing the request. The plugin
asks for native confirmation, then loads only TB Generator's generated
transaction. That transaction checks live identity/revision/dirty state, backs
up, copies sourceTest natively, verifies settings and original getters, and
rolls back on failure. It does not open or close the caller's session. Rejection
leaves the source unchanged; a failure/rollback never starts the pipeline.
Closing Tk cancels a pending request. Reopen ISF from Maestro to obtain a new
bridge after a terminal transaction or stale context.

The dependency's batch run refuses an open setup. Consequently, after a verified
`added` receipt, ISF compares the saved setup against the transaction snapshot,
creates a history-free private execution copy, verifies copy/source hashes,
and passes that copy to the normal TB Generator run/results API and existing
Maestro + generated replica engine. No batch process opens the user's active
setup for writing. The private library is the lock owner for both batch stages.
The source hashes and transaction receipt remain in the materialization cache;
changes after the handoff are rejected. This route is qualified using private
batch caller sessions, including success, rejection and native rollback; real
user sessions are deliberately outside acceptance.

New A/C HB uses TB Generator's `lc_hb_defaults`: linear initial condition,
two-tier, explicit pin nodes, conservative and at least 15 harmonics. Native
readback records the last full inner/outer normalized residuals, positive
fundamental and completed zero-error HB section. This proves a converged HB
periodic solution; `transient_detected=false` is explicit. It does not certify
transient startup or dynamic stability. Existing B HB is preserved.

TB Generator and ISF coordinate native and ISF resources separately. Native setup/RDB hashes are frozen across
the handoff. `materialize` receipts can be reused by `run` after checked-file
validation. Requested thermal without qualified symmetry/source evidence returns
`partial`, retains flicker and exits nonzero. The current integration still reads
TB Generator's version-qualified `native.DEFAULT_RUNTIME` mapping plus
`spec.runtime` so both solver stages use the same environment script. Set the runtime environment script in the testbench generator request.

The preparation page applies its current cds.lib and DUT selection to the submitted
request snapshot, including nested `tbgen_spec` requests. Port readback and edits
use that same nested spec. A cell selection change invalidates pending port
readback callbacks and any previous preparation.


## TB Generator integration

Use the modular VCO TB Generator for the Design Lab, Maestro and OA cell
preparation APIs. Entry A requests an L-b base fixture. Entries A, B and C
complete the selected device, current measurement and HB/HBNOISE/HBXF settings
through `vco_tbgen.isf`. The source-test and open-session transaction APIs keep
their existing request and result schemas.

## Resource wrappers

Set `VCO_ISF_COMMAND_TEMPLATE` to use a local resource wrapper. Without it,
ISF launches its worker directly. The template is split into arguments using
shell-style quoting and run without a shell. Include `{resource}` and exactly
one `{command}` as separate arguments. ISF expands `{command}` to the child
argv and nests wrappers in resource order, with `lib:<library>` outside
`sim:slots` for the native handoff and session guardian. Private optimizer
acquisition uses `sim:slots` alone. The wrapper should run its child in the
foreground and return the child's exit status.

```sh
export VCO_ISF_COMMAND_TEMPLATE='/path/to/resource-wrapper {resource} -- {command}'
```

Configure the TB Generator's own native-stage coordination separately.

## Manual complementary core

Supply both occurrence paths in preparation or completion overrides. Presence
of the `complementary` object enables the mode. Use `null` or omit the field
for the default selector.

```json
{
  "gm": {
    "target_instance": "DUT.MN1",
    "peer_instances": [],
    "complementary": {
      "target_instance": "DUT.MP1",
      "peer_instance": null
    }
  }
}
```

The source netlist must prove opposite MOS polarities and distinct occurrence
paths. A primary peer goes in `gm.peer_instances` (zero or one item). A
secondary peer goes in `gm.complementary.peer_instance`. A peer contributes
only after the corresponding device passes the measured symmetry checks.
The same overrides are accepted by Python `prepare_from_*` and `complete`,
and in the CLI `complete --decisions` JSON under `overrides`.

The bundled Maestro replay configuration uses
`testbenches.vco.mapping_overrides.target_instance` and an optional
`peer_instance` for the first device. Set `analysis.complementary` to the
second device object above and select the generated replica. Complementary
jobs keep separate device workspaces and support replay at one or more
offsets. Start a new complementary job to retry a failed run.

In a complementary canonical `result.json`, `summary` and `points` contain
one sample per device, physical operating point and offset. Each summary
includes `device_instance`, `device_polarity`, `core_operating_point_id` and
`device_closure`. The closure object has `flicker` and `channel_thermal`
records, each containing `isf_dbc_hz`, `hbnoise_dbc_hz`, and `error_db`
(ISF minus HBnoise). Missing or duplicated source references yield a missing
comparison instead of a partial sum. `complementary_core.combined` contains
the power sums at each physical point and offset. Its `closure` covers the
two selected devices. The full modeled budget can also include
symmetry-qualified peers and other supported thermal mechanisms of the
selected devices.

Pass the device occurrence when querying metrics for a physical point.

```python
api.metrics(result_path="result.json", device="DUT.MP1", point="P", offset_hz=100000)
```

```sh
python -B run.py metrics result.json --device DUT.MP1 --point P --offset-hz 100000 --json
```

## Manual asymmetric pair

Use the two-device request shape with `mode: "asymmetric_pair"` and select
two distinct NMOS or two distinct PMOS occurrences. The mode is available in
preparation/completion, generated-replica Maestro replay, and the optimization
reference. A request with any symmetric peer in this mode is rejected.

```json
{
  "gm": {
    "target_instance": "DUT.MN1",
    "peer_instances": [],
    "complementary": {
      "mode": "asymmetric_pair",
      "target_instance": "DUT.MN2"
    }
  }
}
```

For bundled Maestro replay, use `analysis.complementary` with the same
second-device object. The first path goes in
`testbenches.vco.mapping_overrides.target_instance`. The existing
`complementary_core` result envelope has `mode: "asymmetric_pair"` and one
closure sample for each selected occurrence at each physical point and offset.
Its `combined` samples add the two independent powers for each mechanism. The
API retains the default `"complementary"` mode for earlier requests.

## Flicker transfer evidence

Fresh private oscillation runs use the selected primitive's intrinsic D/S
current port and project the measured upper and lower sideband responses onto
PM. A summary's `flicker_transfer` records the selected primitive, node pair,
projection convention and mode (`intrinsic_ds` or `external_ds_fallback`).
The bundle also records this evidence by point in
`metadata.json:flicker_transfer_by_point`. For an external fallback, the
record lists the absent intrinsic traces and contains a notice shown in the
source contribution view. Incomplete nodal sideband evidence fails extraction.
The native fixture's D/S branch remains part of the TB Generator evidence and
the private calculation acquires its own signed transfer.

## Maestro point keys and sweep results

A physical operating point has a stable `pt-…` key. The key is the first 24
hexadecimal digits of SHA-256 over compact, key-sorted ASCII JSON
`[corner, effective_RDB_parameters]`. Values use the SQLite representation
returned by Maestro, including its original string values. The point key
omits model-group metadata (corModelSpec), the history path, native point ID and offset. The source
record retains the exact RDB path, native RDB point ID, test and corner.
Numerical results qualify the sample with a separate `offset_Hz`. For an
offset sweep the sample key is `<point-key>@<offset-token>Hz`. Older results
retain their original keys.

- Setup/desktop runs accept `point_selection` in the run configuration. Omit
  the list for all available points. An explicit list contains unique keys
  from the chosen history. Partial results include a `point_sweep.points`
  manifest with point status and error. Resume retains completed children and
  retries failed or pending children against the frozen history.
- `api.planned_points(spec)` previews the TB Generator condition grid.
  `api.run(..., point_ids=[...])` with these planning keys selects ISF points
  for a new native history. The exact runtime RDB identities replace the
  planned coordinates in results. The Maestro run itself follows its native
  test setup and qualifies coverage before the ISF handoff.
- `api.results(..., point_ids=[...], offsets_hz=[...])` filters numerical
  results by their actual runtime point or sample keys and offsets.
  `api.retry_points(job_id, workspace=...)` queues a retry of a failed ISF
  batch against its existing native history. Job coverage counts completed and
  failed analyses. When requested thermal evidence is incomplete, coverage
  includes a separate `thermal_incomplete` count and the job is partial.
- `metrics --point`, `metrics compare --point/--after-point` and
  `optim evaluate --point` accept a stable key. Pass `--offset-hz` whenever
  the key identifies more than one offset. An ambiguous selector fails. Pass an explicit after key to compare two sweep points at the same offset and modeled source scope.

The command line exposes the same planning and runtime selectors:

```sh
vco-isf points --spec SPEC
vco-isf run --spec SPEC --spec-hash HASH --workspace W --request-id ID --point PLANNING_KEY
vco-isf results --job JOB --workspace W --point RESULT_KEY --offset-hz 10000
vco-isf retry-points --job JOB --workspace W
```

Repeat `--point` or `--offset-hz` to select multiple values.

The native raw directory and its Spectre completion evidence are authoritative
for import. Replay copies the exact point's netlist, restores the reported
parameters and proven simulator temperature, and runs HB, HBnoise, HBXF, DC
noise sampling and the dynamic replica in the selected point's workspace.
