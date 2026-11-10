"""Synchronous command adapters with strict templates and universal dry-run."""

from __future__ import unicode_literals

import os
import re
import shlex
import signal
import string
import subprocess
import time

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

from .errors import (
    AdapterError,
    AdapterExecutionError,
    TemplateValidationError,
    ValidationError,
)
from .models import SubmissionResult, utc_now


TEMPLATE_VARIABLE_WHITELIST = frozenset(
    (
        "job_id",
        "tb",
        "tb_name",
        "testbench",
        "tb_index",
        "test",
        "run_simulation",
        "run_dir",
        "run_root",
        "netlist_dir",
        "work_dir",
        "output_dir",
        "result_dir",
        "results_dir",
        "config_path",
        "bundle_path",
        "project_dir",
        "maestro_dir",
        "rdb_dir",
        "psf_dir",
        "vco_result",
        "replica_workspace",
        "point",
        "corner",
        "scheduler_job_id",
        "submission_key",
    )
)
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TERMINATE_GRACE_S = 2.0
_KILL_GRACE_S = 2.0


def _mapping(value, label):
    if not isinstance(value, Mapping):
        raise ValidationError("%s must be an object" % label)
    return dict(value)


def _allowed_variables(config):
    requested = config.get("template_variables")
    if requested is None:
        return TEMPLATE_VARIABLE_WHITELIST
    if isinstance(requested, str):
        requested = [requested]
    if not isinstance(requested, (list, tuple)):
        raise TemplateValidationError("template_variables must be a list")
    requested = frozenset(str(item) for item in requested)
    disallowed = sorted(requested - TEMPLATE_VARIABLE_WHITELIST)
    if disallowed:
        raise TemplateValidationError(
            "template variable whitelist cannot be expanded",
            details={"disallowed": disallowed},
        )
    return requested


def validate_template_context(variables):
    values = _mapping(variables, "template variables")
    disallowed = sorted(set(values) - TEMPLATE_VARIABLE_WHITELIST)
    if disallowed:
        raise TemplateValidationError(
            "template context contains non-whitelisted variables",
            details={"disallowed": disallowed},
        )
    return values


def render_template(template, variables, allowed_variables=None, shell_quote=False):
    """Render a template after rejecting unknown/dotted/indexed fields."""

    if not isinstance(template, str):
        raise TemplateValidationError("command templates must be strings")
    values = validate_template_context(variables)
    allowed = frozenset(allowed_variables or TEMPLATE_VARIABLE_WHITELIST)
    if not allowed.issubset(TEMPLATE_VARIABLE_WHITELIST):
        raise TemplateValidationError("allowed variable set exceeds the fixed whitelist")

    formatter = string.Formatter()
    fields = []
    try:
        parsed = list(formatter.parse(template))
    except ValueError as exc:
        raise TemplateValidationError("invalid command template: %s" % exc)
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name == "":
            raise TemplateValidationError("positional template fields are not allowed")
        if format_spec or conversion:
            raise TemplateValidationError("template conversions and format specs are not allowed")
        if field_name not in allowed:
            raise TemplateValidationError(
                "template variable is not allowed: %s" % field_name,
                details={"variable": field_name, "allowed": sorted(allowed)},
            )
        if field_name not in values:
            raise TemplateValidationError(
                "template variable is missing: %s" % field_name,
                details={"variable": field_name},
            )
        fields.append(field_name)

    render_values = dict(values)
    if shell_quote:
        for field_name in fields:
            render_values[field_name] = shlex.quote(str(render_values[field_name]))
    try:
        return formatter.vformat(template, (), render_values)
    except (KeyError, ValueError) as exc:  # pragma: no cover - guarded above
        raise TemplateValidationError("cannot render command template: %s" % exc)


def _timeout(config):
    value = config.get("timeout", 3600.0)
    if isinstance(value, bool):
        raise ValidationError("adapter timeout must be numeric")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValidationError("adapter timeout must be numeric")
    if value <= 0.0 or value > 86400.0:
        raise ValidationError("adapter timeout must be between 0 and 86400 seconds")
    return value


def _supports_posix_process_groups():
    return os.name == "posix" and hasattr(os, "killpg")


def _signal_process_tree(process, grouped, force):
    """Signal the complete POSIX process group, with a portable fallback."""

    direct_method = process.kill if force else process.terminate
    if grouped:
        signum = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(process.pid, signum)
            return
        except ProcessLookupError:
            if process.poll() is not None:
                return
        except OSError:
            if process.poll() is not None:
                return
    try:
        direct_method()
    except OSError:
        # The process may have exited between poll/signal operations.
        pass


def _timeout_streams(error):
    stdout = getattr(error, "stdout", None)
    stderr = getattr(error, "stderr", None)
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return str(stdout or ""), str(stderr or "")


def _default_runner(command, cwd, env, timeout):
    grouped = _supports_posix_process_groups()
    popen_options = {"start_new_session": True} if grouped else {}
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        **popen_options
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as timeout_error:
        stdout, stderr = _timeout_streams(timeout_error)
        cleanup_status = "terminated"
        _signal_process_tree(process, grouped, force=False)
        try:
            stdout, stderr = process.communicate(timeout=_TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired as terminate_error:
            stdout, stderr = _timeout_streams(terminate_error)
            cleanup_status = "killed"
            _signal_process_tree(process, grouped, force=True)
            try:
                stdout, stderr = process.communicate(timeout=_KILL_GRACE_S)
            except subprocess.TimeoutExpired as kill_error:
                stdout, stderr = _timeout_streams(kill_error)
                cleanup_status = "kill_wait_expired"
        raise AdapterExecutionError(
            "external command timed out",
            code="ISF-E4004",
            details={
                "timeout": timeout,
                "stdout": stdout,
                "stderr": stderr,
                "termination_scope": "process_group" if grouped else "process",
                "termination": cleanup_status,
            },
        )
    return {
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def _runner_values(completed):
    if isinstance(completed, Mapping):
        return (
            completed.get("returncode", 0),
            completed.get("stdout", ""),
            completed.get("stderr", ""),
        )
    return (
        getattr(completed, "returncode", 0),
        getattr(completed, "stdout", ""),
        getattr(completed, "stderr", ""),
    )


class SubmissionAdapter(object):
    adapter_name = "base"

    def __init__(self, config, runner=None):
        self.config = _mapping(config, "adapter config")
        self.allowed_variables = _allowed_variables(self.config)
        self.timeout = _timeout(self.config)
        self.runner = runner or _default_runner

    def _render(self, template, variables, shell_quote=False):
        return render_template(
            template,
            variables,
            allowed_variables=self.allowed_variables,
            shell_quote=shell_quote,
        )

    def _cwd(self, variables):
        template = self.config.get("cwd")
        if template in (None, ""):
            return None
        return self._render(template, variables)

    def _environment(self, variables):
        inherit = self.config.get("inherit_env", True)
        if not isinstance(inherit, bool):
            raise ValidationError("inherit_env must be a boolean")
        environment = dict(os.environ) if inherit else {}
        configured = self.config.get("env", {})
        configured = _mapping(configured, "adapter env")
        for key, template in configured.items():
            if not isinstance(key, str) or not _ENVIRONMENT_NAME_RE.match(key):
                raise ValidationError("invalid environment variable name: %s" % key)
            environment[key] = self._render(template, variables)
        return environment, sorted(configured)

    def _submit_command(self, command, variables, dry_run, metadata=None):
        if not isinstance(dry_run, bool):
            raise ValidationError("dry_run must be a boolean")
        cwd = self._cwd(variables)
        _environment, configured_env_keys = self._environment(variables)
        result_metadata = dict(metadata or {})
        result_metadata["configured_env_keys"] = configured_env_keys
        started = utc_now()
        if dry_run:
            return SubmissionResult(
                self.adapter_name,
                "dry-run",
                command,
                True,
                cwd=cwd,
                started_at=started,
                finished_at=utc_now(),
                metadata=result_metadata,
            ).to_dict()

        try:
            completed = self.runner(command, cwd, _environment, self.timeout)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterExecutionError(
                "external command could not be started: %s" % exc,
                details={"command": list(command)},
            )
        returncode, stdout, stderr = _runner_values(completed)
        status = "succeeded" if returncode == 0 else "failed"
        return SubmissionResult(
            self.adapter_name,
            status,
            command,
            False,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            started_at=started,
            finished_at=utc_now(),
            metadata=result_metadata,
        ).to_dict()


class RunSimulationAdapter(SubmissionAdapter):
    """Arg-vector adapter for ADE-generated ``runSimulation`` scripts."""

    adapter_name = "runSimulation"

    def __init__(self, config, runner=None):
        super(RunSimulationAdapter, self).__init__(config, runner=runner)
        raw_command = self.config.get("command")
        configured_args = self.config.get("args")
        if isinstance(raw_command, (list, tuple)):
            command_parts = list(raw_command)
        elif isinstance(raw_command, str):
            try:
                command_parts = shlex.split(raw_command)
            except ValueError as exc:
                raise ValidationError("invalid runSimulation command: %s" % exc)
        elif raw_command is None:
            command_parts = [self.config.get("executable", "runSimulation")]
        else:
            raise ValidationError("runSimulation command must be a string or list")
        if configured_args is not None:
            if not isinstance(configured_args, (list, tuple)):
                raise ValidationError("runSimulation args must be a list")
            command_parts.extend(configured_args)
        if not command_parts or any(not isinstance(part, str) for part in command_parts):
            raise ValidationError("runSimulation command entries must be strings")
        self.command_templates = command_parts

    def submit(self, variables, dry_run=True):
        values = validate_template_context(variables)
        command = [self._render(part, values) for part in self.command_templates]
        return self._submit_command(command, values, dry_run)


class CustomShellAdapter(SubmissionAdapter):
    """Explicit shell adapter; inserted template values are shell-quoted."""

    adapter_name = "custom_shell"

    def __init__(self, config, runner=None):
        super(CustomShellAdapter, self).__init__(config, runner=runner)
        self.command_template = self.config.get("command")
        if not isinstance(self.command_template, str) or not self.command_template:
            raise ValidationError("custom shell adapter requires command")
        self.shell = self.config.get("shell", "/bin/sh")
        if not isinstance(self.shell, str) or not self.shell:
            raise ValidationError("custom shell adapter shell must be a path string")

    def submit(self, variables, dry_run=True):
        values = validate_template_context(variables)
        rendered = self._render(self.command_template, values, shell_quote=True)
        command = [self.shell, "-c", rendered]
        return self._submit_command(
            command, values, dry_run, metadata={"rendered_shell_command": rendered}
        )


ShellAdapter = CustomShellAdapter


class ProtocolAdapter(SubmissionAdapter):
    """Small submit/poll/collect adapter for local wrappers and schedulers.

    Commands are argument vectors.  The protocol intentionally stays
    scheduler-neutral and records every rendered command for diagnosis.
    """

    adapter_name = "protocol"

    def __init__(self, config, runner=None):
        super(ProtocolAdapter, self).__init__(config, runner=runner)
        self.submit_templates = self._command("submit_command", required=True)
        self.poll_templates = self._command("poll_command", required=True)
        self.collect_templates = self._command("collect_command", required=False)
        self.cancel_templates = self._command("cancel_command", required=False)
        self.verify_templates = self.config.get("verify_paths", [])
        if isinstance(self.verify_templates, str):
            self.verify_templates = [self.verify_templates]
        if not isinstance(self.verify_templates, (list, tuple)) or any(
            not isinstance(item, str) or not item for item in self.verify_templates
        ):
            raise ValidationError("protocol verify_paths must be a list of paths")
        try:
            self.job_id_pattern = re.compile(
                self.config.get(
                    "job_id_regex",
                    r"(?i)\bjob(?:\s+id)?\s*[=:]?\s*(?P<job_id>[A-Za-z0-9_.:-]+)\b",
                )
            )
            self.success_pattern = re.compile(
                self.config.get(
                    "success_regex", r"(?i)\b(completed|complete|done|success|succeeded)\b"
                )
            )
            self.failure_pattern = re.compile(
                self.config.get(
                    "failure_regex", r"(?i)\b(failed|failure|error|cancelled|canceled)\b"
                )
            )
        except re.error as exc:
            raise ValidationError("invalid protocol regular expression: %s" % exc)
        try:
            self.poll_interval = float(self.config.get("poll_interval_s", 2.0))
        except (TypeError, ValueError):
            raise ValidationError("protocol poll_interval_s must be numeric")
        if self.poll_interval < 0.01 or self.poll_interval > 30.0:
            raise ValidationError(
                "protocol poll_interval_s must be between 0.01 and 30 seconds"
            )

    def _command(self, key, required):
        value = self.config.get(key)
        if value in (None, "") and not required:
            return None
        if not isinstance(value, (list, tuple)) or not value or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise ValidationError(
                "protocol %s must be a non-empty argument-vector list" % key
            )
        return list(value)

    def _render_vector(self, templates, values):
        if templates is None:
            return None
        return [self._render(item, values) for item in templates]

    def _invoke(self, command, cwd, environment, remaining):
        if remaining <= 0:
            raise AdapterExecutionError(
                "protocol adapter timed out",
                code="ISF-E4004",
                details={"timeout": self.timeout},
            )
        try:
            completed = self.runner(command, cwd, environment, remaining)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterExecutionError(
                "protocol command could not be started: %s" % exc,
                details={"command": command},
            )
        returncode, stdout, stderr = _runner_values(completed)
        if returncode != 0:
            raise AdapterExecutionError(
                "protocol command returned non-zero",
                code="ISF-E4005",
                details={
                    "command": command,
                    "returncode": returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )
        return str(stdout or ""), str(stderr or "")

    def submit(self, variables, dry_run=True, resume=None, on_progress=None):
        values = validate_template_context(variables)
        submit_command = self._render_vector(self.submit_templates, values)
        cwd = self._cwd(values)
        environment, configured_env_keys = self._environment(values)
        started = utc_now()
        if dry_run:
            planned = {
                "submit": submit_command,
                "poll": list(self.poll_templates),
                "collect": list(self.collect_templates or []),
                "cancel": list(self.cancel_templates or []),
                "verify_paths": list(self.verify_templates),
                "configured_env_keys": configured_env_keys,
            }
            return SubmissionResult(
                self.adapter_name,
                "dry-run",
                submit_command,
                True,
                cwd=cwd,
                started_at=started,
                finished_at=utc_now(),
                metadata={"protocol": planned},
            ).to_dict()

        deadline = time.monotonic() + self.timeout
        resume = _mapping(resume or {}, "protocol resume metadata")
        scheduler_job_id = resume.get("scheduler_job_id")
        resumed_scheduler_job = scheduler_job_id not in (None, "")
        commands = {
            "submit": None if resumed_scheduler_job else submit_command,
            "poll": [],
            "collect": None,
            "cancel": None,
        }
        stdout = str(resume.get("submit_stdout", ""))
        stderr = str(resume.get("submit_stderr", ""))
        if resumed_scheduler_job:
            scheduler_job_id = str(scheduler_job_id)
            if not re.match(r"^[A-Za-z0-9_.:-]+$", scheduler_job_id):
                raise AdapterExecutionError(
                    "checkpoint scheduler job id is invalid",
                    code="ISF-E5002",
                    details={"scheduler_job_id": scheduler_job_id},
                )
        else:
            stdout, stderr = self._invoke(
                submit_command, cwd, environment, deadline - time.monotonic()
            )
            match = self.job_id_pattern.search(stdout + "\n" + stderr)
            if match is None:
                raise AdapterExecutionError(
                    "protocol submit output did not contain a job id",
                    code="ISF-E4101",
                    details={"stdout": stdout, "stderr": stderr},
                )
            try:
                scheduler_job_id = match.group("job_id")
            except (IndexError, KeyError):
                scheduler_job_id = (
                    match.group(1) if match.groups() else match.group(0)
                )
            if on_progress is not None:
                on_progress(
                    {
                        "scheduler_job_id": scheduler_job_id,
                        "submission_key": values.get("submission_key"),
                        "submit_command": list(submit_command),
                        "submit_stdout": stdout,
                        "submit_stderr": stderr,
                    }
                )
        poll_values = dict(values)
        poll_values["scheduler_job_id"] = scheduler_job_id
        final_output = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self.cancel_templates is not None:
                    cancel = self._render_vector(self.cancel_templates, poll_values)
                    commands["cancel"] = cancel
                    try:
                        self._invoke(cancel, cwd, environment, min(30.0, self.timeout))
                    except AdapterError:
                        pass
                raise AdapterExecutionError(
                    "protocol polling timed out",
                    code="ISF-E4004",
                    details={
                        "scheduler_job_id": scheduler_job_id,
                        "timeout": self.timeout,
                        "commands": commands,
                    },
                )
            poll = self._render_vector(self.poll_templates, poll_values)
            commands["poll"].append(poll)
            poll_stdout, poll_stderr = self._invoke(
                poll, cwd, environment, remaining
            )
            final_output = poll_stdout + "\n" + poll_stderr
            if self.failure_pattern.search(final_output):
                raise AdapterExecutionError(
                    "protocol job reported failure",
                    code="ISF-E4101",
                    details={
                        "scheduler_job_id": scheduler_job_id,
                        "output": final_output,
                        "commands": commands,
                    },
                )
            if self.success_pattern.search(final_output):
                break
            time.sleep(min(self.poll_interval, max(0.01, remaining)))

        collect_stdout = ""
        collect_stderr = ""
        if self.collect_templates is not None:
            collect = self._render_vector(self.collect_templates, poll_values)
            commands["collect"] = collect
            collect_stdout, collect_stderr = self._invoke(
                collect, cwd, environment, deadline - time.monotonic()
            )

        verified_paths = []
        for template in self.verify_templates:
            path = self._render(template, poll_values)
            path = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
            if not os.path.exists(path):
                raise AdapterExecutionError(
                    "protocol output verification failed",
                    code="ISF-E4101",
                    details={"missing": path, "scheduler_job_id": scheduler_job_id},
                )
            verified_paths.append(path)

        return SubmissionResult(
            self.adapter_name,
            "succeeded",
            submit_command,
            False,
            returncode=0,
            stdout=collect_stdout or stdout,
            stderr=collect_stderr or stderr,
            cwd=cwd,
            started_at=started,
            finished_at=utc_now(),
            metadata={
                "scheduler_job_id": scheduler_job_id,
                "submission_key": values.get("submission_key"),
                "resumed_scheduler_job": resumed_scheduler_job,
                "commands": commands,
                "final_poll_output": final_output,
                "verified_paths": verified_paths,
                "configured_env_keys": configured_env_keys,
            },
        ).to_dict()


def adapter_from_config(config, runner=None):
    config = _mapping(config, "adapter config")
    raw_kind = config.get("kind", config.get("type"))
    if not isinstance(raw_kind, str) or not raw_kind:
        raise ValidationError("adapter config requires kind")
    kind = re.sub(r"[^a-z0-9]", "", raw_kind.lower())
    if kind == "runsimulation":
        return RunSimulationAdapter(config, runner=runner)
    if kind in ("shell", "customshell"):
        return CustomShellAdapter(config, runner=runner)
    if kind in ("protocol", "staged", "scheduler"):
        return ProtocolAdapter(config, runner=runner)
    raise ValidationError(
        "unsupported adapter kind: %s" % raw_kind,
        details={"supported": ["runSimulation", "custom_shell", "protocol"]},
    )
