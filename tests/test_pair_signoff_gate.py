"""Regression tests for the formal independent-pair signoff contract."""

from __future__ import unicode_literals

import csv
import io
import json
import math
import os
import tempfile
import unittest

from isf_tool.core.dual_tb import recompute_dual_tb
from isf_tool.core.errors import BundleImportError
from isf_tool.core.orchestrator import _bundle_import

try:
    from .synthetic_fixture import make_canonical
except ImportError:  # unittest discovery loads test modules as top-level names
    from synthetic_fixture import make_canonical


def _canonical(signoff):
    return make_canonical(signoff=signoff)


class PairSignoffGateTests(unittest.TestCase):
    def test_formal_orchestrator_cannot_be_downgraded_by_bundle_option(self):
        canonical = _canonical(signoff=True)
        for row in canonical["vco_results"]:
            row.pop("spectre_pair_pm_asd_V_rtHz")

        with tempfile.TemporaryDirectory() as root:
            for name, rows in (
                ("vco_results.csv", canonical["vco_results"]),
                ("replica_results.csv", canonical["replica_results"]),
                ("workpoint.csv", canonical["workpoint"]),
            ):
                with io.open(os.path.join(root, name), "w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
            with io.open(os.path.join(root, "metadata.json"), "w", encoding="utf-8") as stream:
                json.dump(canonical["metadata"], stream)

            with self.assertRaises(BundleImportError) as raised:
                _bundle_import(
                    {"format": "dual_tb", "path": root, "signoff": False},
                    signoff=True,
                )
            self.assertIn("spectre_pair_pm_asd_V_rtHz", str(raised.exception))

    def test_formal_signoff_emits_complete_pair_fields(self):
        result = recompute_dual_tb(_canonical(signoff=True))

        self.assertEqual(6, len(result["summary"]))
        for row in result["summary"]:
            self.assertTrue(
                all(
                    math.isfinite(row[key])
                    for key in (
                        "predicted_pair_pm_asd_V_rtHz",
                        "spectre_pair_pm_asd_V_rtHz",
                        "pair_error_dB",
                    )
                ),
                row["point"],
            )
            self.assertEqual("independent_power_sum", row["device_combination"])

    def test_formal_signoff_rejects_missing_pair_field_for_one_point(self):
        for key in (
            "second_device_predicted_pm_asd_V_rtHz",
            "spectre_pair_pm_asd_V_rtHz",
        ):
            with self.subTest(key=key):
                canonical = _canonical(signoff=True)
                point = canonical["vco_results"][0]["point"]
                for row in canonical["vco_results"]:
                    if row["point"] == point:
                        row.pop(key)
                with self.assertRaises(BundleImportError) as raised:
                    recompute_dual_tb(canonical)
                self.assertIn(key, str(raised.exception))

    def test_formal_signoff_rejects_missing_or_wrong_pair_metadata(self):
        cases = (
            ("device_count", None),
            ("device_count", 1),
            ("second_device_model", None),
            ("second_device_model", "coherent"),
            ("independent_device_combination", None),
            ("independent_device_combination", "coherent"),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                canonical = _canonical(signoff=True)
                if value is None:
                    canonical["metadata"].pop(key)
                else:
                    canonical["metadata"][key] = value
                with self.assertRaises(BundleImportError) as raised:
                    recompute_dual_tb(canonical)
                self.assertIn(key, raised.exception.details["fields"])

    def test_formal_signoff_recomputes_iq_isf_from_transfer_and_carrier(self):
        canonical = _canonical(signoff=True)
        point = canonical["workpoint"][0]["point"]
        for row in canonical["workpoint"]:
            if row["point"] == point:
                row["isf_i"] = float(row["isf_i"]) * 1.01
                row["product_i"] = float(row["isf_i"]) * float(
                    row["m_dyn_A_rtHz"]
                )
        with self.assertRaisesRegex(
            BundleImportError, "not derivable from signed H/carrier"
        ):
            recompute_dual_tb(canonical)

    def test_development_mode_accepts_missing_pair_contract(self):
        result = recompute_dual_tb(_canonical(signoff=False))

        self.assertEqual(6, len(result["summary"]))
        self.assertTrue(
            all(
                row["predicted_pair_pm_asd_V_rtHz"] is None
                and row["spectre_pair_pm_asd_V_rtHz"] is None
                and row["pair_error_dB"] is None
                for row in result["summary"]
            )
        )


if __name__ == "__main__":
    unittest.main()
