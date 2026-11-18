# VCO ISF Explorer 1.3.0

Analyze every operating point in a Maestro global-variable or corner sweep, or
select a subset before starting. Jobs track each point's progress. Resume
reuses the saved history and keeps completed results when retrying other
points. Each result has a stable point key and a separate offset.

**00 Overview** shows the carrier, noise totals, closure and the modeled
flicker/thermal split for a single point. For a sweep, sort the table or select
a point from the phase-noise plot. The navigator carries the selection across
result views and can group points by corner and variable.

Try **Load synthetic example** for one ideal-LC point or **Example sweep** for
six points across two fictional corners and three bias values. Both run
offline in the desktop. See [screenshots and quick start](README.md).

Use `vco-isf points` to list planning keys, `run --point` to select them,
`results --point` to filter by actual result keys, and `retry-points` to resume
failed ISF points from the same history. See [point keys and API](docs/API.md#maestro-point-keys-and-sweep-results).

Native sweep support was verified on one IC6.1.8/Spectre 18.1 installation with corners × global-variable sweeps.
Use VCO TB Generator 1.1.0 for inherited global model groups.
