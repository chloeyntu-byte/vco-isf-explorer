import unittest
from isf_tool.core.noise_sources import _Reader, parse_psf_ascii
from test_noise_sources import dump

class BusEscapeTests(unittest.TestCase):
    def test_spectre_decimal_register_bus_name_is_not_json_escape(self):
        reader=_Reader(r'"REG_demo:bits\[0\]_return" "literal\\path" "quoted\"name"')
        self.assertEqual(reader.pop(),'REG_demo:bits[0]_return')
        self.assertEqual(reader.pop(),'literal\\path')
        self.assertEqual(reader.pop(),'quoted"name')
        self.assertEqual(_Reader(r'"literal\\[0]"').pop(),r'literal\[0]')
        text=dump().replace('"out"',r'"REG_demo:bits\[0\]_return"')
        result=parse_psf_ascii(text)
        self.assertIn('REG_demo:bits[0]_return',result['traces'])

    def test_invalid_json_control_and_unicode_still_fail(self):
        for text in (r'"bad\q"',r'"bad\uZZZZ"'):
            with self.assertRaises(ValueError):_Reader(text)

if __name__=='__main__':unittest.main()
