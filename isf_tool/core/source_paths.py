"""Fail-closed resolution for design-point source inputs.

The point directory and every consumed nested file must remain inside the
selected source root after resolving symlinks.  Callers use the returned
canonical path, avoiding a second traversal through the unchecked path.
"""

from __future__ import unicode_literals

import os


VCO_NETLIST_RELATIVE = os.path.join("vco", "netlist", "input.scs")


def _real_path(value):
    return os.path.realpath(
        os.path.abspath(os.path.expanduser(str(value)))
    )


def _is_within(parent, child):
    try:
        return os.path.commonpath((parent, child)) == parent
    except (ValueError, OSError):
        return False


def resolve_source_path(source_root, source_dir, relative_path=None):
    """Return a canonical point/file path or raise on any root escape."""
    root = _real_path(source_root)
    source_dir = str(source_dir)
    if not source_dir or os.path.isabs(source_dir):
        raise ValueError("source_dir must be a non-empty relative path")

    point_root = os.path.realpath(os.path.join(root, source_dir))
    if not _is_within(root, point_root):
        raise ValueError(
            "source_dir resolves outside source_root: {}".format(source_dir)
        )
    if relative_path in (None, ""):
        return point_root

    relative_path = str(relative_path)
    if os.path.isabs(relative_path):
        raise ValueError("source input path must be relative")
    target = os.path.realpath(os.path.join(point_root, relative_path))
    if not _is_within(root, target) or not _is_within(point_root, target):
        raise ValueError(
            "source input resolves outside its point/source root: {} / {}"
            .format(source_dir, relative_path)
        )
    return target
