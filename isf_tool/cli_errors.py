"""Common parse and failure envelope for headless commands."""
import argparse
import json

from .core.errors import JobNotFoundError, ValidationError


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def report_error(exc):
    from .api import CONTRACT_VERSION

    code = getattr(exc, 'code', None)
    if isinstance(exc, ImportError):
        code = 'UNSUPPORTED_CAPABILITY'
    elif isinstance(exc, TimeoutError):
        code = code or 'TIMEOUT'
    elif isinstance(exc, RuntimeError):
        code = code or 'EXECUTION_FAILED'
    elif isinstance(exc, JobNotFoundError):
        code = code or 'JOB_NOT_FOUND'
    else:
        code = code or 'INVALID_REQUEST'
    message = str(exc)
    issues = [dict(code=code, severity='error', field='', message=message)]
    if isinstance(exc, ValidationError) and getattr(exc, 'details', None):
        issues[0]['details'] = exc.details
    print(json.dumps(dict(schema='isf.error.v1', contract_version=CONTRACT_VERSION,
                          status='failed', message=message, issues=issues), ensure_ascii=False))
    return {'SOURCE_CHANGED': 6, 'UNSUPPORTED_CAPABILITY': 3, 'TIMEOUT': 5,
            'EXECUTION_FAILED': 4}.get(code, 2)
