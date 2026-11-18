"""Read-only metrics and simulated-result comparison CLI."""
import json

from .core.errors import CoreError

from .cli_errors import Parser, report_error


def main(arguments):
    compare_mode = bool(arguments and arguments[0] == 'compare')
    parser = Parser(prog='vco-isf metrics' + (' compare' if compare_mode else ''),
                    description='Explain changes in two matched simulated results.' if compare_mode else
                                'Compute metrics for one selected result sample. Use metrics compare for a before/after comparison.')
    parser.add_argument('result_path', nargs=None if compare_mode else '?')
    if compare_mode:
        parser.add_argument('after')
        parser.add_argument('--after-point', help='Operating point ID in the after result')
    else:
        parser.add_argument('--job', help='Use a durable job ID in --workspace')
        parser.add_argument('--workspace')
        parser.add_argument('--qualify', action='store_true',
                            help='Include L2 quality and closure fields for optim gate')
    parser.add_argument('--point', help='Sample or operating point ID')
    parser.add_argument('--device', help='Selected MOS occurrence path')
    parser.add_argument('--offset-hz', type=float, help='Exact sample offset in hertz')
    parser.add_argument('--json', action='store_true')
    try:
        args = parser.parse_args(arguments[1:] if compare_mode else arguments)
        if compare_mode:
            from .comparison import compare
            from .core.testbench import read
            output = compare(read(args.result_path), read(args.after), point=args.point,
                             after_point=args.after_point, offset_hz=args.offset_hz, device=args.device)
        else:
            from .api import metrics
            if bool(args.result_path) == bool(args.job):
                raise ValueError('Specify one result path or --job')
            output = metrics(args.job, result_path=args.result_path, workspace=args.workspace,
                             point=args.point, offset_hz=args.offset_hz, device=args.device, qualify=args.qualify)
    except (OSError, ValueError, ImportError, KeyError, RuntimeError, CoreError) as exc:
        return report_error(exc)
    if args.json:
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    elif compare_mode:
        print(output['explanation'])
        for row in output['changes']:
            first, second, delta = ('N/A' if row[key] is None else f"{row[key]:.6g}"
                                    for key in ('before', 'after', 'delta'))
            print(f"{row['metric']} [{row['unit']}]  {first} → {second}  Δ {delta}")
    else:
        print('Point: '+output['source']['point'])
        print('ρ_dyn: %s, ρ_DC: %s, G: %s dB' % (output['flicker']['rho_dyn'],output['flicker']['rho_dc'],output['closure']['gap_db']))
        for card in output['recommendations']:
            print(card['advice']+' · '+card['evidence'])
    return 0
