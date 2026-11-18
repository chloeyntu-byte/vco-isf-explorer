# Automatic setup and expert configuration templates

Normal Tk and Web use does not require creating an engine, profile, or JSON
file.  Select the two Maestro TBs, inspect their generated histories, and use
**Advanced settings → Automatic detection and manual overrides** to review
or correct the detected names and execution settings. The default mapping
view shows the signal roles actually used by the current run, plus unresolved
or invalid choices. The complete inventory and other execution details stay
available in folded diagnostic views. Each editable row
shows its purpose, evidence/source, automatic value, candidates, manual
override, and effective value.  Clearing an override restores automatic
detection; ambiguous candidates require an explicit selection and re-check.

VGS, VDS, and VBS are semantic roles.  The VCO waveform/output name and the
flicker DC variable name can differ; their links are shown in the cross-TB
table.  Nets, outputs and expressions, variables, devices, analyses, and other
discovered names are available in each TB's full catalog as dropdown candidates
or diagnostic evidence; they do not all need to appear in the normal mapping
view. These are name
selections for the tool, not requests to rename the original Maestro objects.

Device connectivity is read from the generated netlist of the selected
history, not directly from an OA schematic binary.  Missing named waveforms
can be derived from confirmed device terminals where supported.  Complex
Calculator expressions are listed as reference evidence and never evaluated;
an unsupported expression selected as an execution waveform must be replaced
with a supported waveform or cleared so confirmed terminals can be used.

The UI stores intentional name choices in each role's `mapping_overrides` and
execution choices in `portable_setup.overrides`.  Detection snapshots are not
manual overrides.  Switching TBs clears old pair-specific settings with a
visible notice; edited or missing names require fresh inspection and preflight.

## Review names and bindings

Select both completed histories, then detect and bind. Review the output
polarity, transfer series, target and cross-TB VGS/VDS/VBS in **Advanced
settings**. Correct a mapping and apply validation, or restore automatic
selection. Show all actual names or diagnostics only when tracing provenance.

Transfer series such as `mag_pxf` and `ph_pxf` or `harm_mag` and `harm_ph`
locate raw responses. Single output names remain valid. Both magnitude and phase
come from the selected complex differential response. A documented private
drain–source fallback is available when appropriate. Stored outputs such as
`HB_PN_VCO_10k` and `noise` serve as comparisons. VGS/VDS/VBS correspond by
electrical role even when the two testbenches use different names.

## Offsets and operating conditions

Use an explicit list such as `1k, 10k, 1M, 100M`, a linear sweep or a
logarithmic sweep. Bare values are in Hz. Examples:

- Linear step of 4 kHz from 1 to 10 kHz gives 1, 5, 9 and 10 kHz.
- Three linear intervals from 1 to 10 kHz give 1, 4, 7 and 10 kHz.
- One point per decade from 1 kHz to 100 MHz gives 1k, 10k, 100k, 1M, 10M and 100M.

Offsets are positive, finite, sorted, and deduplicated, with at most 256 points.
The preview multiplies requested offsets by operating points. Each sample runs
the full scalar calculation. ADE history restores the effective test-specific
parameters to the private netlist. Conflicts and unresolved overrides block the
run. Noise fixture sweeps retain separate bias and non-bias dimensions and
validate temperature and model section. A sweep can stop after the current
offset and resume completed evidence after rechecking inputs and results.

## Optional expert files

Every file in this directory is intentionally fictitious.  Names such as
`PDK_A`, `device_model.scs`, and `generic_nmos` do not describe a foundry kit
or customer design.

If maintaining a custom execution integration, copy the closest template
outside the source checkout, then replace model,
topology, hierarchy, terminal, trace, and executable values with values that
you are authorized to use.  Do not commit a filled private profile to a public
repository.

For this expert-file workflow, copy the engine JSON and its physical profile
as a pair.  A relative `physical_profile_path` is resolved from the engine JSON's
directory; alternatively, place the `physical_profile` object directly in the
engine JSON.  Update both files after moving them to a different host or PDK.

The default path constructs an inspectable execution contract from the bound
history's device hierarchy, four-terminal nodes, models, probes, and selected
signals.  An imported engine/profile supplies that contract instead.  Under
**Advanced settings → Expert file import**, a non-empty engine path overrides
the automatic execution setup.  Its generated suggestions are explicitly
inactive/read-only; clear the expert path to edit and use automatic settings.
Compatibility field names mean:

- `config_path` selects an optional expert engine JSON;
- `physical_profile` / `physical_profile_path` describe the inline or linked
  device/node/probe contract within that expert file; and
- `execution.allow_execute` is request-scoped execution authority. The normal
  UI supplies it when the user explicitly requests preflight/start, not via a
  saved checkbox; save/load/binding does not grant it.

Legacy template matching with `pair_match` remains available for integrations.
It does not prove physical correctness and is not required for the normal
automatically generated setup.  The shipped fictitious templates normally do
not match a private design and must not be run unchanged against one.

`max_harmonic` must be between 1 and 12.  Phase-sign selection enumerates
`2^N` candidates, so preflight and both execution/import backends enforce this
shared safety limit.

For integrations, scalar compatibility remains `analysis.offset.hz`. A
multi-offset request uses `analysis.offsets`; for example:

```json
{
  "analysis": {
    "offsets": {"mode": "list", "values": "1k,10k,1M,100M"}
  }
}
```

`values_hz`, if returned in a resolved configuration, is derived preview data,
not an input override. The service expands the recipe and creates isolated
scalar requests. Do not hand-build a scalar CSV containing several offsets
under the same `point`. See [the format contract](../formats/README.md) for
linear/log recipes, result identities and portable aggregate exports.
