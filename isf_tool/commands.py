"""Headless subcommands. JSON stdout is exactly one response, including errors."""
import contextlib
import json
import sys
from pathlib import Path
from . import api
from .core.testbench import read
from .cli_errors import Parser, report_error

COMMANDS={'prepare','complete','materialize','run','status','results','ui','browse','describe-cell','capabilities'}

COMMAND_HELP = {
    'prepare': ('Prepare and review an ISF testbench request', ('input', 'workspace', 'tbgen-root')),
    'complete': ('Apply decisions to an existing testbench specification', ('spec', 'decisions')),
    'materialize': ('Materialize a reviewed testbench specification', ('spec', 'spec-hash', 'workspace')),
    'run': ('Start a reviewed job and return its durable job ID', ('input', 'spec', 'spec-hash', 'workspace', 'request-id')),
    'status': ('Read the current status of a durable job ID', ('job', 'workspace')),
    'results': ('Read results and issues for a durable job ID', ('job', 'workspace')),
    'ui': ('Open the desktop for a workspace and optional reviewed specification', ('spec', 'workspace')),
    'browse': ('Read the Cadence library catalog from a cds.lib', ('cds-lib', 'workspace', 'tbgen-root')),
    'describe-cell': ('Read DUT terminals from a JSON request', ('input', 'cds-lib', 'workspace', 'tbgen-root')),
    'capabilities': ('Report offline capabilities and native dependencies', ('tbgen-root',)),
}


def main(arguments):
    cmd = arguments[0]
    description, flags = COMMAND_HELP[cmd]
    parser = Parser(prog='vco-isf ' + cmd, description=description)
    if cmd == 'prepare':
        parser.add_argument('entry', choices=['design-lab', 'maestro', 'cell'])
    for flag in flags:
        parser.add_argument('--' + flag, required=flag in {
            'prepare': ('input', 'workspace'), 'status': ('job', 'workspace'),
            'results': ('job', 'workspace'), 'ui': ('workspace',),
            'complete': ('spec',), 'describe-cell': ('input',),
            'materialize': ('spec', 'spec-hash'),
        }.get(cmd, ()))
    if cmd == 'run':
        parser.add_argument('--wait', action='store_true', help='Wait for a terminal job state')
    parser.add_argument('--json', action='store_true', help='Print the JSON response (default)')
    try:
        args=parser.parse_args(arguments[1:])
        with contextlib.redirect_stdout(sys.stderr):
            if cmd=='capabilities':response=api.capabilities(tbgen_root=args.tbgen_root)
            elif cmd=='prepare':
                if not args.entry or not args.input or not args.workspace:raise ValueError('prepare requires entry, --input and --workspace')
                request=read(args.input)
                overrides=request.pop('overrides',None);confirmations=request.pop('confirmations',())
                fn={'design-lab':api.prepare_from_design_lab,'maestro':api.prepare_from_maestro,'cell':api.prepare_from_cell}[args.entry]
                response=fn(request,workspace=args.workspace,tbgen_root=args.tbgen_root,overrides=overrides,confirmations=confirmations)
            elif cmd=='complete':
                decisions=read(args.decisions) if args.decisions else {}
                response=api.complete(args.spec,overrides=decisions.get('overrides',decisions if 'confirmations' not in decisions else {}),confirmations=decisions.get('confirmations',()))
            elif cmd=='materialize':response=api.materialize(args.spec,spec_hash=args.spec_hash,workspace=args.workspace)
            elif cmd=='run':
                req=read(args.input) if args.input else {}
                response=api.run(req.get('spec_path',args.spec),spec_hash=req.get('spec_hash',args.spec_hash),workspace=args.workspace,request_id=req.get('request_id',args.request_id),run_kind=req.get('run_kind','replay'),wait=req.get('wait',args.wait))
            elif cmd in ('status','results'):response=getattr(api,cmd)(args.job,workspace=args.workspace)
            elif cmd=='ui':
                response = api.open_ui(args.spec, workspace=args.workspace)
                if response['status'] == 'failed':
                    from .core.errors import ValidationError
                    log = Path(response['workspace']) / 'ui.log'
                    cause = 'Desktop startup timed out' if response['pid'] else 'Desktop exited before reporting ready'
                    raise ValidationError(f'{cause}. See {log}', code='EXECUTION_FAILED',
                                          details={'log_path': str(log)})
            elif cmd=='browse':response=api.browse_libraries(args.cds_lib,workspace=args.workspace,tbgen_root=args.tbgen_root)
            else:
                req=read(args.input);response=api.describe_cell(req.get('lcv',req.get('dut')),cds_lib=req.get('cds_lib',args.cds_lib),workspace=args.workspace,tbgen_root=args.tbgen_root)
        state=response.get('status',response.get('state',response.get('job',{}).get('state')))
        code=2 if state=='needs_input' else 3 if state=='unsupported' else 4 if state in ('failed','partial') else 0
    except Exception as exc:
        return report_error(exc)
    print(json.dumps(response,ensure_ascii=False,allow_nan=False))
    return code
