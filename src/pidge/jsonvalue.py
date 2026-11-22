"""Helpers for narrowing untyped JSON values to concrete types."""

import json
from collections.abc import Mapping, Sequence
from typing import NoReturn


class JSONShapeError(Exception):
    """A JSON value did not have the expected shape."""


def fail(expected: str, value: object) -> NoReturn:
    """Report a value of the wrong shape; this never returns.

    Args:
        expected: What the value should have been.
        value: The value that was found instead.

    Raises:
        JSONShapeError: Always.
    """
    raise JSONShapeError(f"expected {expected}, got {type(value).__name__}")


def as_dict(value: object) -> Mapping[str, object]:
    """Narrow a value to a string-keyed mapping (JSONShapeError otherwise).

    Args:
        value: The value to narrow.

    Returns:
        A new dictionary with string keys.
    """
    if not isinstance(value, Mapping):
        fail("an object", value)
    return {str(key): item for key, item in value.items()}


def as_list(value: object) -> Sequence[object]:
    """Narrow a value to a sequence (JSONShapeError otherwise).

    Args:
        value: The value to narrow.

    Returns:
        A new list with the same items.
    """
    if not isinstance(value, (list, tuple)):
        fail("an array", value)
    return list(value)


def as_str(value: object) -> str:
    """Narrow a value to a string (JSONShapeError otherwise).

    Args:
        value: The value to narrow.

    Returns:
        The value as a string.
    """
    if not isinstance(value, str):
        fail("a string", value)
    return value


def get_dict(data: Mapping[str, object], key: str) -> Mapping[str, object]:
    """Read an optional object member.

    Args:
        data: The object to read from.
        key: The member name.

    Returns:
        The member, or an empty mapping.
    """
    return as_dict(data.get(key) or {})


def get_list(data: Mapping[str, object], key: str) -> Sequence[object]:
    """Read an optional array member.

    Args:
        data: The object to read from.
        key: The member name.

    Returns:
        The member, or an empty list.
    """
    return as_list(data.get(key) or [])


def string_list(data: Mapping[str, object], key: str) -> Sequence[str]:
    """Read an optional array-of-strings member.

    Args:
        data: The object to read from.
        key: The member name.

    Returns:
        The strings, or an empty list.
    """
    return [as_str(item) for item in get_list(data, key)]


def objects(data: Mapping[str, object], key: str) -> Sequence[Mapping[str, object]]:
    """Read an optional array-of-objects member.

    Args:
        data: The object to read from.
        key: The member name.

    Returns:
        The objects, or an empty list.
    """
    return [as_dict(item) for item in get_list(data, key)]


def pluck(data: Mapping[str, object], key: str, field: str) -> Sequence[str]:
    """Read one string field from each object of an array member.

    Args:
        data: The object to read from.
        key: The array member's name.
        field: The field to read from each element.

    Returns:
        The field values.
    """
    return [get_str(item, field) for item in objects(data, key)]


def error_details(error: Exception) -> Mapping[str, object]:
    """Read the ``Error`` member of a botocore ClientError's response.

    Args:
        error: The exception.

    Returns:
        The error details, or an empty mapping for other exceptions.
    """
    return get_dict(as_dict(getattr(error, "response", {})), "Error")


def get_str(data: Mapping[str, object], key: str, default: str = "") -> str:
    """Read an optional string member.

    Args:
        data: The object to read from.
        key: The member name.
        default: Returned when the member is missing or null.

    Returns:
        The member's string value, or the default.
    """
    value = data.get(key)
    return default if value is None else as_str(value)


def get_number(data: Mapping[str, object], key: str) -> float:
    """Read an optional numeric member, defaulting to zero.

    Args:
        data: The object to read from.
        key: The member name.

    Returns:
        The member's value as a float.
    """
    value = data.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail("a number", value)
    return float(value)


def next_count(data: Mapping[str, object], key: str) -> int:
    """Read a counter member and add one.

    Args:
        data: The object to read from.
        key: The counter's member name.

    Returns:
        The incremented count.
    """
    return int(get_number(data, key)) + 1


def loads_dict(data: bytes | str) -> Mapping[str, object]:
    """Parse JSON text that must be an object.

    Args:
        data: The JSON text.

    Returns:
        The parsed object.
    """
    parsed: object = json.loads(data)
    return as_dict(parsed)


def dumps(value: object) -> bytes:
    """Serialize a value as compact, sorted UTF-8 JSON.

    Args:
        value: The value to serialize.

    Returns:
        The encoded JSON.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
