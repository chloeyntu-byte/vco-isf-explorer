"""One packaged metric glossary for native and Web result views."""

from __future__ import annotations

import copy
import json
from functools import lru_cache

from ..resources import PACKAGE_ROOT


@lru_cache(maxsize=1)
def _catalog():
    path = PACKAGE_ROOT / "ui" / "metric_catalog.json"
    with path.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    metrics = payload["metrics"]
    aliases = {}
    required = {"label", "unit", "meaning", "purpose", "scope", "formula", "aliases"}
    for key, definition in metrics.items():
        if not required <= set(definition):
            raise ValueError("incomplete metric definition: " + key)
        for name in [key, *definition["aliases"]]:
            if name in aliases:
                raise ValueError("duplicate metric alias: " + name)
            aliases[name] = key
    return metrics, aliases


def metric_catalog():
    """Return a defensive {canonical_field: definition} copy."""
    return copy.deepcopy(_catalog()[0])


def metric_definition(field_name):
    """Return {key,label,unit,...} or None; aliases affect help, never data."""
    metrics, aliases = _catalog()
    key = aliases.get(str(field_name))
    return dict(copy.deepcopy(metrics[key]), key=key) if key is not None else None


__all__ = ["metric_definition", "metric_catalog"]
