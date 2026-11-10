"""Keep the public canonical-format guide aligned with the strict importer."""

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class FormatContractDocumentationTests(unittest.TestCase):
    def test_signoff_only_fields_are_named_in_format_guide(self):
        guide = (ROOT / "formats" / "README.md").read_text(encoding="utf-8")
        required_terms = (
            "second_device_predicted_pm_asd_V_rtHz",
            "spectre_pair_pm_asd_V_rtHz",
            "HBAC_norm_mag",
            "HBAC_norm_phase_deg",
            "workpoint.csv",
            "manifest.json",
            "physical-profile ID/hash",
            "ISF-E3001",
            "ISF-E3002",
        )
        for term in required_terms:
            with self.subTest(term=term):
                self.assertIn(term, guide)

    def test_guide_distinguishes_replay_from_owned_signoff(self):
        guide = (ROOT / "formats" / "README.md").read_text(encoding="utf-8")
        self.assertIn("manual import is always treated as `replay`", guide)
        self.assertIn("Only the automatic orchestrator/exporter path", guide)
        self.assertIn("required for every point in `signoff`", guide)


if __name__ == "__main__":
    unittest.main()
