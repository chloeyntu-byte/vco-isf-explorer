"""Shared hard limits for bounded backend computations."""

from __future__ import unicode_literals


# Candidate selection is exhaustive and intentionally exports every sign
# combination.  Keep the hard cap low enough that one request cannot allocate
# an unbounded 2**N result table while preserving the normal N=3 workflow.
MAX_PHASE_CANDIDATE_HARMONICS = 12

# Optional versioned source/band evidence is a JSON cell in canonical CSV.
# Keep this bounded, while allowing more than Python's 128 KiB default.
MAX_CANONICAL_CSV_FIELD_BYTES = 4 * 1024 * 1024
