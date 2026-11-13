"""Compact transfer selectors consume backend membership, not naming guesses."""

import copy
import unittest

from isf_tool.tk.viewmodel import mapping_candidate_names, mapping_selection_details, mapping_review_rows, mapping_source_text


def transfer_field():
    members = [
        {"name": name, "sideband": sideband, "source": "I4", "analysis": "hbxf",
         "expression": 'harmonic(abs(getData("/I4" ?result "hbxf")) %d)' % sideband}
        for name, sideband in (("harm_mag_m1", -1), ("harm_mag_0", 0), ("harm_mag_1", 1))
    ]
    family = {"kind": "transfer_family", "family_name": "harm_mag", "view": "magnitude",
              "source": "I4", "analysis": "hbxf", "anchor_output": "harm_mag_0",
              "sidebands": [-1, 0, 1], "members": members, "errors": []}
    exact = dict(family, kind="transfer_output", members=[members[1]], sidebands=[0])
    return {"key": "transfer_output", "value": "harm_mag", "detected": "harm_mag", "status": "auto",
            "candidates": ["harm_mag", "harm_mag_m1", "harm_mag_0", "harm_mag_1", "other_output"],
            "candidate_details": {"harm_mag": family, "harm_mag_0": exact}, "selected_details": family}


class TransferFamilyViewTests(unittest.TestCase):
    def test_compact_candidates_follow_metadata_without_mutating_raw_catalog(self):
        field = transfer_field()
        before = copy.deepcopy(field)
        self.assertEqual(["harm_mag", "other_output"], mapping_candidate_names(field))
        self.assertEqual(before, field)

    def test_selected_exact_member_stays_available_and_diagnostics_expand_all(self):
        field = transfer_field()
        self.assertEqual(["harm_mag", "harm_mag_0", "other_output"], mapping_candidate_names(field, "harm_mag_0"))
        self.assertEqual(field["candidates"], mapping_candidate_names(field, expand_families=True))
        self.assertEqual("my_exact_name", mapping_candidate_names(field, "my_exact_name")[-1])

    def test_no_name_only_grouping_or_hiding_of_unresolved_series(self):
        field = transfer_field()
        del field["candidate_details"]
        self.assertEqual(field["candidates"], mapping_candidate_names(field))
        field = transfer_field()
        field["candidate_details"]["harm_mag"]["errors"] = ["Member expression is unreadable"]
        self.assertEqual(field["candidates"], mapping_candidate_names(field))

    def test_details_show_actual_source_analysis_expression_and_sideband(self):
        field = transfer_field()
        text = "\n".join(mapping_selection_details(field))
        for expected in ("Family harm_mag", "3 discovered members", "Original current source: I4", "Analysis: hbxf",
                         "Read anchor: harm_mag_0", "harm_mag_m1 → Sideband -1", "sideband = k − 1",
                         'getData("/I4"', "the user confirms the circuit mapping"):
            self.assertIn(expected, text)
        self.assertNotIn("branch verification passed", text)

    def test_precise_selection_not_relabelled_family_and_unknown_does_not_reuse_old_evidence(self):
        field = transfer_field()
        exact = "\n".join(mapping_selection_details(field, "harm_mag_0"))
        self.assertIn("Exact output: harm_mag_0", exact)
        self.assertIn("awaits application and validation", exact)
        unknown = "\n".join(mapping_selection_details(field, "unknown_name"))
        self.assertIn("New input", unknown)
        self.assertNotIn("I4", unknown)
        self.assertNotIn("harm_mag", unknown)

    def test_read_errors_and_missing_members_remain_visible(self):
        field = transfer_field()
        field["candidate_details"]["harm_mag"].update(errors=["Member read failed"], missing_sidebands=[2, 3])
        text = "\n".join(mapping_selection_details(field))
        self.assertIn("Read notes: Member read failed", text)
        self.assertIn("Missing sidebands: 2, 3", text)

    def test_nonzero_representative_and_informational_notes_do_not_expand_or_fail_series(self):
        field = transfer_field()
        field["candidate_details"]["harm_mag"].update(anchor_output="harm_mag_1", notes=["no sidebands0; use the user-selected read anchor"])
        self.assertEqual(["harm_mag", "other_output"], mapping_candidate_names(field))
        text = "\n".join(mapping_selection_details(field))
        self.assertIn("Read anchor: harm_mag_1", text)
        self.assertIn("Family notes (does not block execution)", text)
        self.assertNotIn("Sideband 0 anchor", text)

    def test_review_preserves_family_contract_and_legacy_fields_are_unchanged(self):
        field = transfer_field()
        report = {"roles": {"vco": {"mapping_fields": [field]}}}
        row = mapping_review_rows(report)[0]
        self.assertEqual(field["candidate_details"], row["candidate_details"])
        self.assertEqual([], mapping_selection_details({"key": "vgs_output", "value": "HB_TIME_VGS"}))
        self.assertEqual(["A1", "B2"], mapping_candidate_names({"candidates": ["A1", "B2"], "value": "A1"}))

    def test_test_local_value_does_not_request_global_migration_when_global_evidence_exists(self):
        for evidence in ({"shadows_global": True}, {"global_expression": "0"}):
            text = mapping_source_text({"selected_details": {"scope": "test", **evidence}})
            self.assertIn("Global already exists / Test retain the same-name value", text)
            self.assertNotIn("must be global", text)
        self.assertIn("must be global", mapping_source_text({"selected_details": {"scope": "test"}}))

    def test_phase_role_consumption_is_shown_even_when_candidate_metadata_has_no_role_annotation(self):
        field = transfer_field()
        field["key"] = "transfer_phase_output"
        field["selected_details"] = copy.deepcopy(field["selected_details"])
        notice = "Reference source; magnitude and phase are remeasured as a full complex response from the actual transfer source, does not combine different sources. "
        field["selected_details"]["role_consumption"] = notice
        self.assertNotIn("role_consumption", field["candidate_details"]["harm_mag"])
        self.assertIn("Actually used: " + notice, mapping_selection_details(field))


if __name__ == "__main__":
    unittest.main()
