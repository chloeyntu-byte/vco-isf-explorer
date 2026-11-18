"""Generate portable closed-form metrics fixtures without simulator data."""
import math


def sample(name, corner, modulation, phase):
    point = f'{corner}@1000000Hz'
    carrier = 1.0
    frequency = 2e9
    offset = 1e6
    target, peer = 'XCORE.M1', 'XCORE.M2'
    powers = [5e-15, 5e-15]
    sources = [dict(device=device, mechanism='id', psd_V2_Hz=power,
                    input_band_contributions=[
                        dict(input_band=1, psd_V2_Hz=power * .75),
                        dict(input_band=-2, psd_V2_Hz=power * .25)])
               for device, power in zip((target, peer), powers)]
    model = 'symmetric_independent'
    thermal = dict(schema_version=1, status='available', noise_quantity='PM_SSB',
                   pm_validation={'passed': True}, scope='gm_pair_and_lc_network',
                   pair_model=model, passive_scope='passive_lc_branches_v2',
                   target_noise_trace=target, peer_noise_trace=peer, sources=sources,
                   predicted_psd_V2_Hz=sum(powers), carrier_peak_V=carrier,
                   f0_Hz=frequency, offset_Hz=offset, input_max_harmonic=3,
                   requested_max_harmonic=3)
    pair_asd = 2e-7
    total_asd = 3e-7 if name == "A" else 4e-7
    summary = dict(point=point, operating_point_id=corner, corner=corner,
                   operating_parameters={'code': 1}, f0_Hz=frequency,
                   offset_Hz=offset, carrier_peak_V=carrier, pair_model=model,
                   target_noise_trace=target, replica_source='generated' if name in ('A', 'B-final') else 'manual',
                   numerical_quality_pass=True,
                   pm_magnitude_projection_valid=True, hbac_secondary_validation_pass=True,
                   dynamic_pm_asd_V_rtHz=1e-7, spectre_fn_pm_asd_V_rtHz=1.01e-7,
                   predicted_pair_pm_asd_V_rtHz=pair_asd, spectre_pair_pm_asd_V_rtHz=pair_asd*1.01,
                   spectre_total_pm_asd_V_rtHz=total_asd,
                   spectre_noise_reference=dict(schema_version=1, scope='whole_vco'),
                   spectre_noise_contributions=[
                        dict(device=target, mechanism='fn', psd_V2_Hz=2e-14),
                        dict(device=peer, mechanism='fn', psd_V2_Hz=2e-14),
                        dict(device='RL', mechanism='rn', psd_V2_Hz=1e-13),
                        *[dict(device=s['device'], mechanism='id', psd_V2_Hz=s['psd_V2_Hz'])
                          for s in sources]],
                   flicker_pair_identity=dict(target_noise_trace=target, peer_noise_trace=peer,
                                              pair_model=model), thermal_noise=thermal)
    workpoint = []
    for index in range(257):
        theta = 2 * math.pi * index / 256
        vdiff = math.sin(theta)
        common_mode = .08 * math.sin(2 * theta + .2)
        gamma = -math.cos(theta)
        dc = 1 + modulation * math.cos(theta)
        dyn = 1 + modulation * math.cos(theta + phase)
        workpoint.append(dict(sample=index, wt_rad=theta, Vdiff_V=vdiff,
                              VD_V=1 + vdiff/2 + common_mode, VDN_V=1 - vdiff/2 + common_mode,
                              ID_main_A=1e-3 * dc, isf_i=gamma, isf_q=0,
                              m_DC_A_rtHz=dc, m_dyn_A_rtHz=dyn,
                              product_i=gamma * dyn, product_q=0))
    harmonics = [dict(k=k, Q_real_V_rtHz=pair_asd * weight, Q_imag_V_rtHz=0.,
                      delta_k_deg=math.degrees(phase) if k == 1 else 0., A_k=1.)
                 for k, weight in enumerate((.1, .8, -.05, .15))]
    return dict(name=name, point=point, result=dict(summary=[summary], harmonics=harmonics,
                                                  workpoint=workpoint))


def records():
    return [sample('new', 'band_high', .3, .2), sample('new', 'band_low', .33, .2),
            sample('old', 'band_high', .4, -.2), sample('old', 'band_low', .44, -.2),
            sample('A', 'nominal', .3, .2), sample('B-final', 'band_high', .36, .1)]
