# Contributing

This project uses the [MIT license](LICENSE).

Contributions must contain only material the contributor is authorized to
publish.  Never add process models, PDK/OA content, real netlists, RDB/PSF,
screenshots with private paths, license information, customer topology, or
simulator-derived performance data without explicit release approval.

Run `python -B -m unittest discover -s tests` and `node --test tests/*.js`.
Run `xvfb-run -a env ISF_GUI_TEST=1 python -B -m unittest discover -s tests`
for desktop coverage. Set `VCO_TBGEN_ROOT` to the optional sibling checkout
to exercise the integration contracts. CI also checks that a clean wheel can
replay the synthetic example. Unit tests use synthetic data and run without EDA.

Maintainers run a separate strict release scan against the entire tree with
the reviewed exceptions in `.oss-allow`. The private identifier dictionaries
used by the release scanner are kept outside the public repository and CI.
