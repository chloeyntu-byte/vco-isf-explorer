"""Typed, web-friendly exceptions with stable local error codes."""

from __future__ import unicode_literals


class CoreError(Exception):
    """Base error carrying a stable machine-readable code."""

    default_code = "ISF-E9000"
    error_kind = "core_error"

    def __init__(self, message, code=None, details=None):
        super(CoreError, self).__init__(message)
        self.message = str(message)
        self.code = code or self.default_code
        self.kind = self.error_kind
        self.details = dict(details or {})

    def to_dict(self):
        from .error_catalog import enrich_error

        payload = {
            "code": self.code,
            "kind": self.kind,
            "message": self.message,
            "details": dict(self.details),
        }
        try:
            return enrich_error(payload)
        except Exception:
            # Error serialization is a trust boundary.  Optional local help
            # must not replace the real code/message if the catalog is broken.
            return payload


class ValidationError(CoreError):
    default_code = "ISF-E1001"
    error_kind = "validation_error"


class ProfileError(ValidationError):
    default_code = "ISF-E1002"
    error_kind = "profile_error"


class PreflightError(ValidationError):
    default_code = "ISF-E1101"
    error_kind = "preflight_error"


class DiscoveryError(CoreError):
    default_code = "ISF-E2001"
    error_kind = "discovery_error"


class BundleImportError(CoreError):
    default_code = "ISF-E3001"
    error_kind = "bundle_import_error"


class AdapterError(CoreError):
    default_code = "ISF-E4001"
    error_kind = "adapter_error"


class TemplateValidationError(AdapterError):
    default_code = "ISF-E4002"
    error_kind = "template_validation_error"


class AdapterExecutionError(AdapterError):
    default_code = "ISF-E4003"
    error_kind = "adapter_execution_error"


class ResumeError(CoreError):
    default_code = "ISF-E5001"
    error_kind = "resume_error"


class JobNotFoundError(CoreError):
    default_code = "ISF-E6001"
    error_kind = "job_not_found"


class JobNotReadyError(CoreError):
    default_code = "ISF-E6002"
    error_kind = "job_not_ready"


class JobFailedError(CoreError):
    default_code = "ISF-E6003"
    error_kind = "job_failed"
