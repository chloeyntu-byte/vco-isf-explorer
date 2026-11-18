import unittest
from isf_tool.core.maestro_outputs import attach_outputs,output_metrics
class OutputTests(unittest.TestCase):
 def test_exact_point_verified_values_and_delta(self):
  name='ISF_HBNOISE_PN_10000Hz_dBc_Hz';defs=[dict(name=name,units='dBc/Hz')]
  r=dict(summary=[dict(corner='TT',operating_parameters=dict(VDD=1.8,L='200p'),offset_Hz=1e4,spectre_total_dBc_Hz=-100.03)])
  p=dict(corner='TT',variables=dict(VDD=1.8,L=2e-10),outputs={name:dict(value=-100)},output_qualification={name:dict(status='verified')})
  v=attach_outputs(r,[p],defs)['summary'][0]
  self.assertAlmostEqual(v['maestro_pn_reference_delta_db'],.03);self.assertEqual(output_metrics(v)['pn_dbc_hz'],-100)
  self.assertNotIn('maestro_outputs',r['summary'][0])
  self.assertNotIn('maestro_outputs',attach_outputs(r,[p,p],defs)['summary'][0])
 def test_missing_nonfinite_unverified_never_zero(self):
  s=dict(corner='TT',offset_Hz=1e4)
  self.assertIsNone(output_metrics(s)['pn_dbc_hz'])
  p=dict(corner='TT',outputs={'F':dict(value=float('nan'))},output_qualification={'F':dict(status='verified')})
  self.assertEqual(attach_outputs(dict(summary=[s]),[p],[dict(name='F')])['summary'][0]['maestro_outputs'],{})

 def test_native_outputs_match_exact_psf_and_equivalent_model_encoding(self):
  from isf_tool.core.testbench import tbgen
  from isf_tool.core.errors import ValidationError
  try:
   tbgen()
   from vco_tbgen.effective import reported_corner_models
  except (ImportError, ValidationError):
   self.skipTest('TB Generator model group support unavailable')
  summary=dict(corner='ExampleFast',point_identity={'psf_dir':'/fixture/2/psf'},
               operating_parameters={'VDD':1.25,'corModelSpec':'File=All#Global#/fixture/device.scs Section="example_fast";'})
  source=dict(corner='ExampleFast',psf_dir='/fixture/2/psf',
              variables={'VDD':1.25,'corModelSpec':'(("/fixture/device.scs" "example_fast"))'},
              outputs={'F':dict(value=6e9)},output_qualification={'F':dict(status='verified')})
  self.assertTrue(reported_corner_models(source['variables']['corModelSpec']))
  decoy=dict(source,psf_dir='/fixture/1/psf')
  result=attach_outputs(dict(summary=[summary]),[decoy,source],[dict(name='F',units='Hz')])
  self.assertEqual(result['summary'][0]['maestro_outputs']['F']['value'],6e9)
  result=attach_outputs(dict(summary=[summary]),[decoy],[dict(name='F')])
  self.assertNotIn('maestro_outputs',result['summary'][0])
