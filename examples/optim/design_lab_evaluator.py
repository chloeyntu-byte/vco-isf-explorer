"""Importable optimize adapter factory; caller supplies references and constraints.

Usage in the host process (its existing code remains unchanged)::

    from isf_tool.optim.adapters import design_lab_evaluator
    evaluator = design_lab_evaluator(reference_factory, level='L0',
        calibration=family_calibration, constraints=electrical_constraints)
    observation = evaluator(project, generated_directory)

reference_factory(project, directory) returns a vco.isf-design-ref.v1 with an owned
workspace, external design_lab_root and measured LUT/nodes. electrical_constraints
returns status/feasible/total_violation plus qualified L2 isf_metrics and measured
loopgain/frequency/power evidence. No implicit all-pass constraint callback.
"""
from isf_tool.optim.adapters import design_lab_evaluator


def make_evaluator(reference_factory, electrical_constraints, calibration):
    return design_lab_evaluator(reference_factory, level='L0', calibration=calibration,
                                constraints=electrical_constraints)
