"""Display evidence must retain missing channels and never turn old bindings into closure."""

import unittest

from isf_tool.tk.viewmodel import active_mapping_rows, mapping_review_rows, mapping_source_text, waveform_exchange_view


class MappingEvidenceTests(unittest.TestCase):
    def test_required_missing_rows_cannot_hide_in_diagnostics(self):
        rows = active_mapping_rows({"roles": {"vco": {"mapping_fields": [
            {"key": "vbs_output", "required": True, "active": False, "group": "execution_detail", "status": "missing"}
        ]}}})
        self.assertEqual(["vbs_output"], [row["key"] for row in rows])

    def test_provenance_distinguishes_named_output_from_script_and_scope(self):
        self.assertIn("Maestro Named output", mapping_source_text({"maestro_presence": "named_output"}))
        derived = mapping_source_text({"maestro_presence": "script_derived", "effective_derivation": "V(B)-V(S)", "scope": "test"})
        self.assertIn("V(B)-V(S)", derived)
        self.assertIn("must be global", derived)
        self.assertNotIn("Maestro Named output", derived)

    def test_new_exchange_schema_keeps_destination_relative_path_and_blocker(self):
        self.assertIn("not checked", waveform_exchange_view({"ok": True, "bindings": {"vco": {"binding_fingerprint": "old"}}})["summary"])
        view = waveform_exchange_view({"waveform_exchange": {
            "ok": False, "checked": "static_prepare", "summary": "Actual body-source waveform missing", "path_policy": "relative_to_run",
            "checks": [{"id": "vbs", "status": "fail", "detail": "VBS terminals not saved"}],
            "rows": [{"semantic": "VBS", "source_kind": "script_derived", "source_expression": "V(B)-V(S)",
                      "destination_variable": "bias_c", "pwl_variable": "wave_c", "destination_source": "port_c",
                      "relative_file": "waveforms/vbs.pwl", "scope": "global", "status": "fail", "detail": "Source data missing"}]
        }})
        self.assertEqual("wave_c → port_c", view["rows"][0]["destination"])
        self.assertEqual("waveforms/vbs.pwl", view["rows"][0]["path_policy"])
        self.assertIn("V(B)-V(S)", view["rows"][0]["source"])
        self.assertIn("Source data missing", view["rows"][0]["status"])
        self.assertEqual(["VBS terminals not saved"], view["checks"])

    def test_script_generated_fallback_is_visible_without_inventing_a_maestro_variable(self):
        report = {"roles": {"flicker": {"mapping_fields": [{"key": "vgs_pwl_variable", "value": None,
                  "maestro_presence": "script_derived", "candidate_kind": "variable"}]}},
                  "waveform_exchange": {"rows": [{"semantic": "VGS", "pwl_variable": "ISF_VGS_PWL_FILE"}]}}
        row = mapping_review_rows(report)[0]
        self.assertEqual("ISF_VGS_PWL_FILE", row["effective_value"])
        self.assertIsNone(row["value"])
        self.assertIn("file variable", mapping_source_text(row))
        self.assertNotIn("terminal difference", mapping_source_text(row))
        self.assertNotIn("effective_value", report["roles"]["flicker"]["mapping_fields"][0])


if __name__ == "__main__":
    unittest.main()
