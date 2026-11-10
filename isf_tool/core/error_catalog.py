"""Local, dependency-free error-code catalog."""

from __future__ import unicode_literals

import copy
import io
import json
import os
import threading


_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ui",
    "error_catalog.json",
)
_LOCK = threading.RLock()
_CACHE = None


def catalog_path():
    return _CATALOG_PATH


def load_error_catalog(refresh=False):
    global _CACHE
    with _LOCK:
        if _CACHE is None or refresh:
            with io.open(_CATALOG_PATH, "r", encoding="utf-8") as stream:
                value = json.load(stream)
            entries = value.get("entries", [])
            if not isinstance(entries, list):
                raise ValueError("error catalog entries must be a list")
            codes = [str(item.get("code", "")) for item in entries]
            if not codes or any(not code for code in codes) or len(codes) != len(set(codes)):
                raise ValueError("error catalog contains missing or duplicate codes")
            _CACHE = value
        return copy.deepcopy(_CACHE)


def error_entry(code):
    code = str(code or "")
    try:
        entries = load_error_catalog().get("entries", [])
    except (IOError, OSError, ValueError, TypeError):
        # Diagnostics must never hide the original failure.  A damaged or
        # missing local catalog simply means the API omits optional help text.
        return None
    for entry in entries:
        if entry.get("code") == code:
            return copy.deepcopy(entry)
    return None


def enrich_error(value):
    payload = dict(value or {})
    code = payload.get("code", "ISF-E9000")
    try:
        entry = error_entry(code)
        if entry is not None:
            payload.setdefault(
                "help",
                {
                    "title": entry.get("title"),
                    "summary": entry.get("summary"),
                    "actions": list(entry.get("actions", [])),
                    "severity": entry.get("severity", "error"),
                },
            )
    except Exception:
        pass
    return payload
