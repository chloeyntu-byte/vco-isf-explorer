"""Typed PSF source/transfer/output parsing, unrelated to the flicker parser."""

import unittest

from isf_tool.core.noise_sources import (parse_psf_ascii, noise_sample, noise_quantity,
    quantity_as_psd, contributor_records, classify_mechanism)


def dump():
    return '''HEADER
"analysis type" "xfersrcs.pnoise_hbnoise"
TYPE
"sweep" FLOAT DOUBLE
"out_asd" FLOAT DOUBLE PROP("units" "V/sqrt(Hz)")
"mos" STRUCT(
"fn" FLOAT DOUBLE PROP("units" "V^2/Hz")
"id.src" FLOAT DOUBLE PROP("units" "A^2/Hz")
"id.xf2" FLOAT DOUBLE PROP("units" "(V^2/Hz)/(A^2/Hz)")
"id.out" FLOAT DOUBLE PROP("units" "V^2/Hz")
"total" FLOAT DOUBLE PROP("units" "V^2/Hz")
) PROP("master" "b3v3")
"diode" STRUCT(
"id" FLOAT DOUBLE PROP("units" "V^2/Hz")
) PROP("master" "diode")
"ind" STRUCT(
"rn.src" FLOAT DOUBLE PROP("units" "A^2/Hz")
) PROP("master" "inductor")
"complex_transfer" COMPLEX DOUBLE PROP("units" "V/A")
SWEEP
"relative frequency" "sweep"
TRACE
"X7.M1" "mos"
"X7.D1" "diode"
"L3" "ind"
"out" "out_asd"
"H" "complex_transfer"
VALUE
"relative frequency" 10000
"X7.M1" (1e-12 2e-18 9 18e-18 1.000018e-12)
"X7.D1" (7e-20)
"L3" (8e-21)
"out" 4e-6
"H" (2 -3)
END
'''


class NoiseSourceTests(unittest.TestCase):
    def test_named_fields_preserve_units_master_and_complex_values(self):
        parsed = parse_psf_ascii(dump())
        self.assertEqual('b3v3', parsed['types']['mos']['master'])
        self.assertEqual('id.src', parsed['types']['mos']['fields'][1]['name'])
        self.assertEqual('A^2/Hz', parsed['types']['mos']['fields'][1]['units'])
        self.assertEqual(2-3j, noise_sample(parsed, 10000)['values']['H'])
        self.assertEqual(2e-18, noise_quantity(parsed, 10000, 'X7.M1', 'id.src', expected_domain='A')['psd'])

    def test_asd_is_squared_once_and_psd_is_not(self):
        self.assertAlmostEqual(16e-12, noise_quantity(parse_psf_ascii(dump()),10000,'out')['psd'], delta=1e-25)
        self.assertEqual(2e-18, quantity_as_psd(2e-18,'A^2/Hz')['psd'])
        self.assertEqual(2e-9, quantity_as_psd(2e-9,'A/sqrt(Hz)')['asd'])

    def test_source_output_power_gain_and_diode_id_remain_distinct(self):
        records = contributor_records(parse_psf_ascii(dump()),10000)
        by = {(r['device'],r['field']):r for r in records}
        self.assertEqual('channel_thermal', by['X7.M1','id.src']['noise_family'])
        self.assertEqual('shot', by['X7.D1','id']['noise_family'])
        self.assertEqual('power_gain',by['X7.M1','id.xf2']['quantity_kind'])
        self.assertEqual(9,by['X7.M1','id.xf2']['power_gain'])
        self.assertNotIn('psd',by['X7.M1','id.xf2'])
        self.assertEqual('A^2/Hz',by['L3','rn.src']['units'])
        self.assertEqual('A',by['L3','rn.src']['domain'])
        self.assertNotIn(('X7.M1','total'),by)
        self.assertEqual(len(records)+1,len(contributor_records(parse_psf_ascii(dump()),10000,include_totals=True)))
        self.assertEqual('unclassified',classify_mechanism('mystery','id'))

    def test_frequency_must_be_exact_and_unique(self):
        p=parse_psf_ascii(dump())
        with self.assertRaises(ValueError): noise_sample(p,9999)
        p['samples'].append(p['samples'][0])
        with self.assertRaises(ValueError): noise_sample(p,10000)

    def test_negative_frequency_is_retained_for_signed_transfer_measurements(self):
        p=parse_psf_ascii(dump().replace('"relative frequency" 10000','"relative frequency" -10000'))
        self.assertEqual(-10000,noise_sample(p,-10000)['frequency'])

    def test_missing_fields_units_and_wrong_domain_fail_explicitly(self):
        p=parse_psf_ascii(dump())
        calls=[lambda:noise_quantity(p,10000,'X7.M1','missing'),
               lambda:noise_quantity(p,10000,'X7.M1'),
               lambda:noise_quantity(p,10000,'out',expected_domain='A'),
               lambda:quantity_as_psd(1,None),lambda:quantity_as_psd(-1,'A^2/Hz'),
               lambda:quantity_as_psd(float('nan'),'A^2/Hz')]
        for call in calls:
            with self.subTest(call=call),self.assertRaises(ValueError):call()

    def test_truncated_nonfinite_duplicate_and_malformed_struct_fail(self):
        texts=[dump().replace('END',''),dump().replace('4e-6','nan'),
               dump().replace('"out" 4e-6','"out" 4e-6\n"out" 2e-6'),
               dump().replace('(7e-20)','(7e-20 3e-20)'),
               dump().replace('"X7.D1" "diode"','"X7.D1" "unknown"')]
        for text in texts:
            with self.subTest(text=text[-90:]),self.assertRaises(ValueError):parse_psf_ascii(text)


if __name__=='__main__':
    unittest.main()
