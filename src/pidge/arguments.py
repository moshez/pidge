"""Validation of MCP tool arguments."""

import datetime
from collections.abc import Callable, Mapping, Sequence

from . import jsonvalue, ulid


class ToolError(Exception):
    """A tool call failed in a way the caller should see."""


def narrowed[T](
    read: Callable[[Mapping[str, object], str], T],
    args: Mapping[str, object],
    name: str,
    expected: str,
) -> T:
    """Read an argument, turning a shape error into a tool error.

    Args:
        read: Reads and narrows the argument.
        args: The arguments.
        name: The argument name.
        expected: What the argument should be, for the error message.

    Returns:
        The value.

    Raises:
        ToolError: If the value has the wrong shape.
    """
    try:
        return read(args, name)
    except jsonvalue.JSONShapeError:
        raise ToolError(f"{name} must be {expected}") from None


def text(args: Mapping[str, object], name: str) -> str:
    """Read an optional string argument.

    Args:
        args: The arguments.
        name: The argument name.

    Returns:
        The value, or an empty string.
    """
    return narrowed(jsonvalue.get_str, args, name, "a string")


def required_text(args: Mapping[str, object], name: str) -> str:
    """Read a required, non-empty string argument.

    Args:
        args: The arguments.
        name: The argument name.

    Returns:
        The value.

    Raises:
        ToolError: If the value is missing or empty.
    """
    value = text(args, name)
    if not value:
        raise ToolError(f"{name} is required")
    return value


def message_id(args: Mapping[str, object], name: str) -> str:
    """Read a message ID argument.

    Args:
        args: The arguments.
        name: The argument name.

    Returns:
        The ULID.

    Raises:
        ToolError: If the value is not a ULID.
    """
    value = required_text(args, name)
    if not ulid.is_ulid(value):
        raise ToolError(f"{name} must be a message id")
    return value


def strings(args: Mapping[str, object], name: str) -> Sequence[str]:
    """Read an optional list-of-strings argument.

    Args:
        args: The arguments.
        name: The argument name.

    Returns:
        The strings, or an empty list.
    """
    return narrowed(jsonvalue.string_list, args, name, "a list of strings")


def flag(args: Mapping[str, object], name: str) -> bool | None:
    """Read an optional boolean argument.

    Args:
        args: The arguments.
        name: The argument name.

    Returns:
        The value, or None if absent.

    Raises:
        ToolError: If the value is not a boolean.
    """
    value = args.get(name)
    if value is None or isinstance(value, bool):
        return value
    raise ToolError(f"{name} must be a boolean")


def bounded(args: Mapping[str, object], name: str, limit: int) -> int:
    """Read an optional integer argument between 1 and a limit.

    Args:
        args: The arguments.
        name: The argument name.
        limit: The maximum, also the default.

    Returns:
        The value.

    Raises:
        ToolError: If the value is not an integer in range.
    """
    value = args.get(name, limit)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= limit:
        raise ToolError(f"{name} must be an integer from 1 to {limit}")
    return value


def date(args: Mapping[str, object], name: str) -> int:
    """Read an optional ISO 8601 date or date-time argument.

    Args:
        args: The arguments.
        name: The argument name.

    Returns:
        Epoch seconds (UTC when no zone is given), or 0 if absent.

    Raises:
        ToolError: If the value is not an ISO 8601 date.
    """
    value = text(args, name)
    if not value:
        return 0
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{name} must be an ISO 8601 date") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return int(parsed.timestamp())
