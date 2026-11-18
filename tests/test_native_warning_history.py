"""A quoted Verilog-A $fatal warning is not an executed fatal diagnostic."""
from pathlib import Path
import tempfile
import unittest
from isf_tool.core.discovery import _spectre_terminal_status

class NativeWarningTests(unittest.TestCase):
    def test_warning_source_does_not_hide_zero_error_terminal_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'spectre.out';p.write_text('WARNING (VACOMP-2366): "$fatal(1,\"Invalid code\")<<--? ;"\n"/private/model.va", line 14: the $fatal function always behaves as though the message level value is 0.\nspectre completes with 0 errors, 5 warnings, and 0 notices.\n')
            self.assertTrue(_spectre_terminal_status(str(p),65536)['zero_errors'])
            p.write_text('FATAL: simulation failed\nspectre completes with 0 errors, 5 warnings, and 0 notices.\n')
            self.assertFalse(_spectre_terminal_status(str(p),65536)['zero_errors'])

if __name__=='__main__':unittest.main()
