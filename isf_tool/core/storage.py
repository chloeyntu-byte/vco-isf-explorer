"""Storage estimates and read-only archive/prune previews."""
import json
import os
from pathlib import Path

from .orchestrator import _tree_identity


def projected_bytes(offsets, operating_points):
    """Use the documented 65 MiB per sample sizing scenario."""
    return int(offsets) * int(operating_points) * 65 * (1024 ** 2)


def _size(directory):
    total = 0
    for folder, _, names in os.walk(directory):
        for name in names:
            path = Path(folder) / name
            if not path.is_symlink() and path.is_file():
                total += path.stat().st_size
    return total


def preview(workspace, job_id, *, api=False, terminal=False):
    """Find duplicate child exports only where an intact source bundle survives."""
    category = 'api-jobs' if api else 'jobs'
    parent = Path(workspace).resolve() / category
    job = parent / job_id
    if job.resolve().parent != parent or not job.is_dir():
        raise ValueError('Job directory is unavailable in this workspace')
    archive_bytes = _size(job)
    candidates = []
    if not terminal:
        return dict(archive_bytes=archive_bytes, candidates=candidates, prune_bytes=0,
                    note='Wait until the job is terminal before archiving or pruning.')
    roots = (job / 'isf-workspace' / 'jobs').glob('*') if api else (job,)
    for root in roots:
        export = root / 'canonical'
        sweep_path = export / 'offset_sweep.json'
        if not sweep_path.is_file():
            continue
        sweep = json.loads(sweep_path.read_text(encoding='utf-8'))
        for child in sweep.get('children', []):
            if child.get('status') != 'succeeded' or not child.get('bundle_path'):
                continue
            duplicate = (export / child['bundle_path']).resolve()
            child_id = child.get('job_id')
            child_workspace = child.get('workspace')
            if not child_id or Path(child_id).name != child_id or child_id in ('.', '..') or not child_workspace:
                continue
            original = (Path(child_workspace) / 'jobs' / child_id / 'canonical').resolve()
            if (not duplicate.is_relative_to((export / 'offsets').resolve())
                    or not original.is_relative_to(job) or original == duplicate
                    or not duplicate.is_dir() or not original.is_dir()):
                continue
            expected = child.get('bundle_tree_sha256')
            if expected and all(_tree_identity(path)['sha256'] == expected for path in (original, duplicate)):
                candidates.append(dict(path=str(duplicate), bytes=_size(duplicate), keep=str(original)))
    return dict(archive_bytes=archive_bytes, candidates=candidates, prune_bytes=0,
                note='Archive a completed job as a whole. Retain its manifests, results, checkpoints, simulation evidence and native child directories.')
