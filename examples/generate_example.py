#!/usr/bin/env python3
"""Regenerate the synthetic ideal-LC replay bundle in ``examples/ideal_lc``.

Usage::

    python3 examples/generate_example.py [--single | --sweep] [output_directory]

The bundle is an analytic ideal-LC model (see ``isf_tool/example_data.py``);
it contains no simulator or PDK data.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from isf_tool.example_data import build  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--single", action="store_true", help="one ideal-LC point")
    modes.add_argument("--sweep", action="store_true", help="six points across two fictional corners")
    parser.add_argument("output_directory", nargs="?")
    args = parser.parse_args(argv)
    name = "ideal_lc_sweep" if args.sweep else "ideal_lc_single" if args.single else "ideal_lc"
    print(build(args.output_directory or os.path.join(HERE, name), sweep=args.sweep, single=args.single))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
