#!/usr/bin/env python3
"""Regenerate the synthetic ideal-LC replay bundle in ``examples/ideal_lc``.

Usage::

    python3 examples/generate_example.py [output_directory]

The bundle is an analytic ideal-LC model (see ``isf_tool/example_data.py``);
it contains no simulator or PDK data.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from isf_tool.example_data import build  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    print(build(argv[0] if argv else os.path.join(HERE, "ideal_lc")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
