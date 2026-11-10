# Contributing

Contributions are welcome under the MIT license (see `LICENSE`).

Contributions must contain only material you are authorized to publish. Never add
PDK or vendor process models, OA content, real netlists, RDB/PSF results,
screenshots showing private paths, license server information, customer topology or
simulator-derived performance data. Unit tests and examples must use synthetic
data and must not invoke a real EDA tool.

Keep changes small and preserve fail-closed behavior. Before sending a change,
run the offline checks listed in the README (`python -m unittest discover -s tests`
and, if you touch the Web UI, the Node tests).
