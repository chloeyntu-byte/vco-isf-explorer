"""Independent raw-PSF total/contributor and portable evidence regressions."""

import copy
import csv
import importlib.util
import io
import json
import math
from pathlib import Path
import unittest

from isf_tool.core.dual_tb import recompute_dual_tb, spectre_noise_evidence
from isf_tool.core.errors import BundleImportError
from test_scalar_manual_reference import canonical_fixture


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENGINE = load_script("total_noise_engine_tests", "dynamic_isf_closure_engine.py")
EXPORT = load_script("total_noise_export_tests", "export_audited_bundle.py")


def noise_dump():
    # Deliberately non-MOS ordering: fn is first, not positional index 3.
    return '''HEADER
"analysis type" "pm.pnoise_hbnoise"
"analysis name" "hbnoise"
"positive output signal" "OUTP"
"negative output signal" "OUTN"
"relharmnum" 1
TYPE
"V/sqrt(Hz)" FLOAT DOUBLE PROP(
"units" "V/sqrt(Hz)"
)
"mos_custom" STRUCT(
"fn" FLOAT DOUBLE PROP(
"units" "V^2/Hz"
)
"id" FLOAT DOUBLE PROP(
"units" "V^2/Hz"
)
"total" FLOAT DOUBLE PROP(
"units" "V^2/Hz"
)
) PROP(
"key" "inst"
)
"resistor" STRUCT(
"rn" FLOAT DOUBLE PROP(
"units" "V^2/Hz"
)
"total" FLOAT DOUBLE PROP(
"units" "V^2/Hz"
)
) PROP(
"key" "inst"
)
SWEEP
"relative frequency" "sweep"
TRACE
"XCORE.M7" "mos_custom"
"R17" "resistor"
"out" "V/sqrt(Hz)"
VALUE
"relative frequency" 10000
"XCORE.M7" (
4e-12
9e-12
13e-12
)
"R17" (
1e-12
1e-12
)
"out" %.16e
END
''' % math.sqrt(14e-12)


def evidence():
    return ENGINE.parse_hbnoise_total(noise_dump(), 10000, source_path="owned/main_vco/psf/hbnoise.pm.pnoise_hbnoise")


class RawTotalNoiseTests(unittest.TestCase):
    def test_output_is_asd_not_square_rooted_twice(self):
        result = evidence()
        self.assertAlmostEqual(result["spectre_total_pm_asd_V_rtHz"], math.sqrt(14e-12), places=17)
        self.assertAlmostEqual(ENGINE.dBc_from_pm_asd(result["spectre_total_pm_asd_V_rtHz"], math.sqrt(2)), 10 * math.log10(14e-12), places=11)

    def test_all_device_types_use_named_mechanisms_and_exclude_totals(self):
        result = evidence()
        actual = {(item["device"], item["mechanism"]): item["psd_V2_Hz"] for item in result["spectre_noise_contributions"]}
        self.assertEqual({("XCORE.M7", "fn"): 4e-12, ("XCORE.M7", "id"): 9e-12, ("R17", "rn"): 1e-12}, actual)
        provenance = result["spectre_noise_reference"]
        self.assertEqual(2, len(provenance["device_totals"]))
        self.assertEqual("whole_vco", provenance["scope"])
        self.assertEqual("OUTP", provenance["positive_output"])
        self.assertAlmostEqual(provenance["mechanism_sum_minus_total_psd_V2_Hz"], 0, places=25)

    def test_total_is_not_replaced_by_contributor_sum(self):
        text = noise_dump().replace('"out" %.16e' % math.sqrt(14e-12), '"out" 8e-6')
        result = ENGINE.parse_hbnoise_total(text, 10000)
        self.assertEqual(8e-6, result["spectre_total_pm_asd_V_rtHz"])
        self.assertNotEqual(0, result["spectre_noise_reference"]["mechanism_sum_minus_total_psd_V2_Hz"])

    def test_wrong_total_units_or_am_dataset_are_not_mislabeled_pm(self):
        for text in (noise_dump().replace('"units" "V/sqrt(Hz)"', '"units" "V^2/Hz"'),
                     noise_dump().replace('"pm.pnoise_hbnoise"', '"am.pnoise_hbnoise"')):
            with self.assertRaises(RuntimeError):
                ENGINE.parse_hbnoise_total(text, 10000)

    def test_missing_or_duplicate_frequency_is_not_nearest_guessed(self):
        with self.assertRaises(RuntimeError):
            ENGINE.parse_hbnoise_total(noise_dump(), 20000)
        text = noise_dump().replace("END", '"relative frequency" 10000\n"out" 5e-6\nEND')
        with self.assertRaises(RuntimeError):
            ENGINE.parse_hbnoise_total(text, 10000)

    def test_negative_psd_and_schema_mismatch_are_rejected(self):
        for text in (noise_dump().replace("4e-12", "-4e-12"),
                     noise_dump().replace("4e-12\n9e-12\n13e-12", "4e-12\n13e-12")):
            with self.assertRaises(RuntimeError):
                ENGINE.parse_hbnoise_total(text, 10000)


class TotalNoisePortableEvidenceTests(unittest.TestCase):
    def test_csv_json_round_trip_retains_source_total_and_contributors(self):
        original = evidence()
        fields = EXPORT.noise_evidence_csv_fields(original)
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerow(fields)
        decoded = spectre_noise_evidence(next(csv.DictReader(io.StringIO(stream.getvalue()))))
        self.assertEqual(original, decoded)

    def test_old_single_device_total_is_never_promoted(self):
        old = {"spectre_total_pm_asd_V_rtHz": 1e-6,
               "spectre_fn_pm_asd_V_rtHz": 0.9e-6}
        decoded = spectre_noise_evidence(old)
        self.assertIsNone(decoded["spectre_total_pm_asd_V_rtHz"])
        self.assertEqual([], decoded["spectre_noise_contributions"])
        self.assertIsNone(EXPORT.noise_evidence_csv_fields(old)["spectre_total_pm_asd_V_rtHz"])

    def test_bad_evidence_cannot_silently_become_empty_reference(self):
        bad = copy.deepcopy(evidence())
        bad["spectre_noise_contributions"][0]["asd_V_rtHz"] = 12.0
        with self.assertRaises(BundleImportError):
            spectre_noise_evidence(bad)
        bad = EXPORT.noise_evidence_csv_fields(evidence())
        bad["spectre_noise_reference_json"] = "{bad json"
        with self.assertRaises(BundleImportError):
            spectre_noise_evidence(bad)

    def test_explicit_reference_frequency_and_total_agree_with_sample(self):
        bad = copy.deepcopy(evidence())
        bad["offset_Hz"] = 100000
        with self.assertRaisesRegex(BundleImportError, "offset mismatch"):
            spectre_noise_evidence(bad)
        bad = copy.deepcopy(evidence())
        bad["spectre_total_pm_asd_V_rtHz"] *= 2
        with self.assertRaisesRegex(BundleImportError, "ASD/PSD mismatch"):
            spectre_noise_evidence(bad)

    def test_boolean_schema_version_is_not_version_one(self):
        bad = copy.deepcopy(evidence())
        bad["spectre_noise_reference"]["schema_version"] = True
        self.assertIsNone(spectre_noise_evidence(bad)["spectre_total_pm_asd_V_rtHz"])

    def test_additional_noise_evidence_does_not_change_isf_calculation(self):
        fixture = canonical_fixture()
        previous = recompute_dual_tb(fixture)["summary"][0]
        new_fields = EXPORT.noise_evidence_csv_fields(evidence())
        for row in fixture["vco_results"]:
            row.update(new_fields)
        updated = recompute_dual_tb(fixture)["summary"][0]
        for key in ("dynamic_dBc_Hz", "dynamic_pm_asd_V_rtHz", "dynamic_error_dB", "dc_dBc_Hz", "delay_dBc_Hz", "physical_vdyn_real_V_rtHz", "physical_vdyn_imag_V_rtHz", "chosen_signs", "candidate_score"):
            self.assertEqual(previous[key], updated[key], key)
        self.assertEqual(evidence()["spectre_noise_contributions"], updated["spectre_noise_contributions"])
        self.assertEqual(evidence()["spectre_total_pm_asd_V_rtHz"], updated["spectre_total_pm_asd_V_rtHz"])

    def test_mixed_scalar_total_evidence_is_not_merged(self):
        fixture = canonical_fixture()
        for row in fixture["vco_results"]:
            row.update(EXPORT.noise_evidence_csv_fields(evidence()))
        fixture["vco_results"][-1]["spectre_total_pm_asd_V_rtHz"] = 100.0
        fixture["vco_results"][-1]["spectre_noise_reference_json"] = json.dumps({"schema_version": 1, "scope": "whole_vco"})
        with self.assertRaisesRegex(BundleImportError, "inconsistent total noise"):
            recompute_dual_tb(fixture)


if __name__ == "__main__":
    unittest.main()
