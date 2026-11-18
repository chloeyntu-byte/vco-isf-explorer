"""Optional evaluator wrappers. Upstream optimizers are never imported here."""
from __future__ import annotations
from .api import evaluate as isf_evaluate, gate, number


def design_lab_evaluator(reference_factory, *, level='L0', calibration=None, constraints=None):
    """Return evaluate(project, dir). L0 feasibility is only proxy feasibility.

    constraints(project, dir) must return the independent measured startup/frequency/
    power observation. Missing constraints are infeasible, rather than implicitly OK.
    """
    def evaluate(project, directory):
        try:
            result = isf_evaluate(reference_factory(project, directory), level=level, calibration=calibration)
            checks = constraints(project, directory) if constraints else None
            if not isinstance(checks, dict) or checks.get('status')!='completed':
                return dict(status='not_evaluated', feasible=False, total_violation=1., merit=None, objectives=[],
                            violations=['Independent electrical constraints unavailable'], evidence=result)
            violation = number(checks.get('total_violation'))
            if violation is None or violation<0 or type(checks.get('feasible')) is not bool or checks['feasible']!=(violation==0):
                raise ValueError('Invalid independent constraints observation')
            sample = result['metrics'][0]
            score = number(sample.get('rho_hat') if level=='L0' else sample.get('pn_dbc_hz') if level=='L1' else (sample.get('flicker') or {}).get('rho_dyn'))
            if score is None:
                return dict(status='not_evaluated', feasible=False, total_violation=1., merit=None, objectives=[],
                            violations=['Objective unavailable'], evidence=dict(isf=result, electrical=checks))
            if level in ('L0', 'L2'):
                score = abs(score)
            reasons = list(checks.get('violations', []))
            if level in ('L0','L2'):
                # L0 reuses independent L2 evidence; a new L2 must qualify itself.
                # A good baseline must never override a failed candidate closure.
                decision = gate(result if level=='L2' else checks.get('isf_metrics', {}))
                if not decision['isf_shaping_allowed']:
                    violation += 1.; reasons.append(decision['reason'])
            return dict(status='completed', feasible=violation==0, total_violation=violation,
                        merit=float(score), objectives=[float(score)], violations=reasons,
                        evidence=dict(isf=result, electrical=checks),
                        qualification='proxy_ranking_only' if level!='L1' else 'single_code_HBnoise')
        except (ValueError, RuntimeError, OSError, KeyError) as exc:
            return dict(status='failed', feasible=False, total_violation=1., merit=None, objectives=[],
                        violations=[str(exc)], evidence={})
    return evaluate


def ask_tell(optimizer, make_candidate, evaluator, *, budget=7, batch_size=1):
    """Population-generator-compatible ask/tell with explicit evidence."""
    if type(budget) is not int or type(batch_size) is not int or not 1<=batch_size<=budget:
        raise ValueError('Require 1 <= batch_size <= budget')
    history = []
    while len(history)<budget:
        values = optimizer.ask(min(batch_size, budget-len(history)))
        if not values:
            raise RuntimeError('Optimizer returned no candidates before budget completion')
        if len(values)>min(batch_size,budget-len(history)):
            raise ValueError('Optimizer exceeded requested batch size')
        completed = []
        for knobs in values:
            project, directory = make_candidate(knobs, len(history))
            observation = evaluator(project, directory)
            row = dict(candidate_id='isf-%04d'%len(history),values=knobs,status=observation['status'],
                       metrics=dict(merit=observation.get('merit'), objectives=[{'score':s} for s in observation.get('objectives',[])],
                                    feasible=observation['feasible'], total_violation=observation['total_violation']),
                       evidence=observation.get('evidence',{}),directory=str(directory))
            if row['status']=='completed':
                if number(row['metrics']['merit']) is None:
                    raise ValueError('Completed evaluator merit must be finite')
                completed.append(row)
            history.append(row)
        optimizer.tell(completed)
    return history
