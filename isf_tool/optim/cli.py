"""JSON-only CLI for independent optim calls."""
from ..cli_errors import Parser, report_error
from ..core.errors import CoreError
import json
from .api import evaluate, calibrate, gate, rank_knobs, read, resolve


def main(argv=None):
    parser = Parser(prog='vco-isf optim', description='Evaluate or qualify saved ISF evidence.')
    commands = parser.add_subparsers(dest='action',required=True)
    e = commands.add_parser('evaluate', help='Evaluate a design reference or selected result sample'); e.add_argument('design_ref', nargs='?'); e.add_argument('--level',choices=['L0','L1','L2'],default='L0')
    e.add_argument('--calibration'); e.add_argument('--offset-hz',action='append',type=float)
    e.add_argument('--job', help='Select a saved job ID')
    e.add_argument('--workspace', help='Workspace containing --job')
    e.add_argument('--point', help='Sample or operating point ID')
    e.add_argument('--device', help='Select one MOS occurrence from a saved two-device L2 result')
    c = commands.add_parser('calibrate', help='Calibrate from one measured sample'); c.add_argument('design_ref')
    g = commands.add_parser('gate', help='Apply the shaping eligibility gate to qualified L2 metrics'); g.add_argument('metrics')
    r = commands.add_parser('rank-knobs', help='Compute recipe-based sensitivity estimates'); r.add_argument('metrics'); r.add_argument('recipe')
    try:
        args = parser.parse_args(argv)
        if args.action=='evaluate':
            if bool(args.design_ref) == bool(args.job):
                raise ValueError('Specify one design reference/result or --job')
            if args.job:
                if not args.workspace:
                    raise ValueError('--job requires --workspace')
                from ..api import results
                ref = resolve({'result': results(args.job, workspace=args.workspace)['result']})
                if not isinstance(ref.get('result'), dict):
                    raise ValueError('Job has no saved result')
            else:
                ref = resolve(args.design_ref)
            if args.point:
                ref['point'] = args.point
            offsets = args.offset_hz or ref.get('offsets_hz')
            if not offsets and ref.get('offset_hz') is not None:
                offsets = [ref['offset_hz']]
            if not offsets and args.level in ('L1', 'L2') and ref['kind'] == 'artifact':
                from .api import _raw
                from ..metrics import select_point
                raw = _raw(ref)
                if raw.get('status') != 'measured':
                    _, _, sample = select_point(raw, point=ref.get('point'), device=args.device)
                    offsets = [sample.get('offset_Hz', sample.get('offset_hz'))]
            out = evaluate(ref, level=args.level, calibration=args.calibration, offsets_hz=offsets or (1e5,), device=args.device)
        elif args.action=='calibrate':
            out = calibrate(args.design_ref)
        elif args.action=='gate':
            out = gate(read(args.metrics))
        else:
            out = rank_knobs(read(args.metrics),read(args.recipe))
        print(json.dumps(out,ensure_ascii=False,allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, ImportError, CoreError) as exc:
        return report_error(exc)
