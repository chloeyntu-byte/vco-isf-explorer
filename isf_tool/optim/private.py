"""Generated Design Lab netlist -> existing private ISF engine, no OA writes.

This adapter only freezes inputs and calls the bundled engine. It does not
implement ISF, noise models, thermal acquisition, or pair-symmetry assumptions.
"""
import hashlib
import importlib.util
from pathlib import Path
import shutil
from .api import read, write
from ..resources import script_path


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(generated, ref, work, offsets, site):
    complementary = ref.get('complementary')
    if complementary is None:
        return _run_device(generated, ref, work, offsets, site)
    from ..core.component_selection import core_mode, core_targets, validate_core_targets
    from ..core.complementary import combine_devices
    from ..core.dual_tb import import_dual_tb_bundle
    from ..core.spectre_topology import SpectreTopology

    targets = core_targets(ref.get('gm'), ref.get('peer'), complementary)
    mode = core_mode(complementary)
    polarities = validate_core_targets(SpectreTopology(Path(generated)/'tb.scs'), targets, mode=mode)
    entries = []
    for index, ((target, peer), polarity) in enumerate(zip(targets, polarities), 1):
        directory = work / ('device-%d' % index)
        response = _run_device(generated, dict(ref, gm=target, peer=peer, core_device=True),
                               directory, offsets, site)
        path = response['result_path']
        result = import_dual_tb_bundle(Path(path).parent)
        result['run_kind'] = 'replay'
        entries.append((dict(instance=target, peer_instance=peer, polarity=polarity, result_path=path), result))
    result_path = work/'canonical'/'result.json'
    write(result_path, combine_devices(entries, mode=mode))
    return dict(result_path=str(result_path))


def _run_device(generated, ref, work, offsets, site):
    if len(offsets)!=1:
        raise ValueError('Private generated L2 currently accepts exactly one offset')
    from ..core.signal_mapping import discover_signal_mapping
    from ..core.portable_setup import build_portable_setup
    from ..core.generated_flicker import generated_fixture
    generated = Path(generated)
    manifest = read(generated/'manifest.json')
    for name, sha in manifest['files'].items():
        if hashlib.sha256((generated/name).read_bytes()).hexdigest()!=sha:
            raise ValueError('Generated input changed: '+name)
    for name, sha in manifest.get('model_files',{}).items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=sha:
            raise ValueError('Model input changed: '+name)
    source = work/'sources'/'point'/'vco'/'netlist'
    source.mkdir(parents=True)
    for name in ('circuit.scs','models.scs'):
        shutil.copy2(generated/name, source/name)
    shutil.copy2(generated/'tb.scs', source/'input.scs')
    project = read(generated/'project.json')
    test = dict(name='generated',variables=[],outputs=[],analyses=[
        dict(name='hb',type='hb',enabled=True,fields=dict(p='OUTP',n='OUTN')),
        dict(name='hbnoise',type='hbnoise',enabled=True,fields=dict(p='OUTP',n='OUTN'))],
        models=project['binding']['model_includes'])
    history = dict(source_netlists=[dict(path=str(source/'input.scs'))])
    if not ref.get('gm'):
        raise ValueError('Private L2 requires an explicitly selected gm occurrence')
    overrides = dict(target_instance=ref['gm'],output_p='OUTP',output_n='OUTN')
    if ref.get('core_device') is True:
        overrides['core_device'] = True
    if ref.get('peer'):
        overrides['peer_instance'] = ref['peer']
    role = discover_signal_mapping(test,'vco',history,overrides)
    role.update(selected_test=test,selected_history=history)
    env = dict(executables=dict(spectre=str(site.spectre()),psf=ref['psf']),cadence_env=str(site.cadence_env()))
    setup = build_portable_setup(dict(roles=dict(vco=role),pair=dict(replica_source='generated')),
        dict(analysis=dict(offset=dict(hz=offsets[0]),max_harmonic=ref.get('max_harmonic',3))), env)
    write(work/'detected-setup.json',setup)
    if not setup['ok']:
        raise ValueError('Private generated bindings invalid: '+str(setup['errors']))
    cfg = setup['engine']
    fixture = work/'sources'/'point'/'flicker'/'netlist'/'input.scs'
    generated_fixture(source/'input.scs',ref['gm'],offsets[0],destination=fixture)
    point = dict(k=1.,corner='pilot',source_dir='point',flicker_template_netlist=str(fixture),
                 mos_source_netlist=str(source/'input.scs'),mos_instance_path=ref['gm'])
    cfg.update(source_root=str(work/'sources'),points=[point],flicker_template_netlist=str(fixture),
               dc_noise_samples=ref.get('dc_noise_samples',24),hb_max_harmonic=project.get('simulation',{}).get('harmonics',15),
               main_max_sideband=project.get('simulation',{}).get('harmonics',15))
    write(work/'engine.json',cfg)
    engine_path = script_path('dynamic_isf_closure_engine.py')
    engine = load(engine_path,'isf_optim_engine')
    engine.main(['--config',str(work/'engine.json'),'--run-root',str(work/'simulation')])
    export = load(script_path('export_audited_bundle.py'),'isf_optim_export')
    export.main(['--engine',str(engine_path),'--config',str(work/'engine.json'),
                 '--run-root',str(work/'simulation'),'--output',str(work/'canonical'),'--non-signoff'])
    return dict(result_path=str(work/'canonical'/'result.json'))
