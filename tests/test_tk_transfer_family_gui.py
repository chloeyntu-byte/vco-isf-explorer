"""Opt-in isolated Tk test; does not connect to the user's UI or an EDA process."""

import os
import tkinter as tk
import unittest
from unittest import mock

from isf_tool.tk.app import IsfApplication
from tests.test_tk_transfer_family_view import transfer_field


@unittest.skipUnless(os.environ.get("ISF_GUI_TEST") == "1", "requires an explicitly selected GUI display")
class NativeTransferFamilyTests(unittest.TestCase):
    def test_compact_default_exact_edit_and_diagnostic_expansion_preserve_overrides(self):
        root = tk.Tk()
        root.title("ISF temporary transfer selector verification")
        root.lower()
        controller = mock.Mock(workspace="/tmp/isolated-ui-transfer-test")
        app = None
        try:
            with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
                app = IsfApplication(root, controller)
            field = transfer_field()
            report = {"roles": {"vco": {"mapping_fields": [field]}}}
            app._render_mapping_report(report)
            box = app._mapping_inputs["vco:transfer_output"]
            self.assertEqual("harm_mag", box.get())
            self.assertEqual(("harm_mag", "other_output"), tuple(box.cget("values")))
            self.assertEqual({}, app._mapping_overrides["vco"])
            box.set("harm_mag_0")
            self.assertEqual("harm_mag_0", app._collect_fields()["vco_mapping_overrides"]["transfer_output"])
            self.assertIn("Exact output: harm_mag_0", app.mapping_detail_var.get())
            app._render_mapping_report(report)
            self.assertIn("harm_mag_0", app._mapping_inputs["vco:transfer_output"].cget("values"))
            self.assertNotIn("harm_mag_m1", app._mapping_inputs["vco:transfer_output"].cget("values"))
            app.show_all_mappings_var.set(True)
            app._render_mapping_report(report)
            self.assertEqual(tuple(field["candidates"]), tuple(app._mapping_inputs["vco:transfer_output"].cget("values")))
            self.assertEqual("harm_mag_0", app._mapping_overrides["vco"]["transfer_output"])
            app._reset_mapping_input("vco:transfer_output")
            self.assertEqual({}, app._mapping_overrides["vco"])
            self.assertEqual("harm_mag", app._mapping_inputs["vco:transfer_output"].get())
            self.assertIn("Read anchor: harm_mag_0", app.mapping_detail_var.get())
        finally:
            if app is not None:
                app.queue.close()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
