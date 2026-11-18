"""Optional argv wrapper for locally managed native execution resources."""

import os
import shlex

from .errors import ValidationError


ENVIRONMENT_VARIABLE = "VCO_ISF_COMMAND_TEMPLATE"


def wrap_command(command: list[str], *resources: str) -> list[str]:
    """Wrap a child command once per resource, in the order given."""
    template = os.environ.get(ENVIRONMENT_VARIABLE, "")
    if not template.strip():
        return command

    try:
        tokens = shlex.split(template)
    except ValueError as exc:
        raise ValidationError(f"Invalid {ENVIRONMENT_VARIABLE}: {exc}") from exc
    if (
        tokens.count("{command}") != 1
        or "{resource}" not in tokens
        or tokens[0] in ("", "{resource}", "{command}")
    ):
        raise ValidationError(
            f"{ENVIRONMENT_VARIABLE} requires a wrapper executable, {{resource}} and exactly one {{command}} as separate arguments"
        )

    index = tokens.index("{command}")
    argv = command
    for resource in reversed(resources):
        wrapper = [resource if token == "{resource}" else token for token in tokens]
        argv = wrapper[:index] + argv + wrapper[index + 1:]
    return argv
