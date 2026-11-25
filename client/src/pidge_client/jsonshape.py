"""Narrow untyped JSON values to concrete types."""

import json
from collections.abc import Mapping, Sequence
from typing import NoReturn


class ShapeError(Exception):
    """A JSON value did not have the expected shape."""


def fail(expected: str, value: object) -> NoReturn:
    """Report a value of the wrong shape; this never returns.

    Args:
        expected: What the value should have been.
        value: The value found instead.

    Raises:
        ShapeError: Always.
    """
    raise ShapeError(f"expected {expected}, got {type(value).__name__}")


def as_dict(value: object) -> Mapping[str, object]:
    """Narrow a value to a string-keyed mapping.

    Args:
        value: The value.

    Returns:
        A new dictionary with string keys.
    """
    if not isinstance(value, Mapping):
        fail("an object", value)
    return {str(key): item for key, item in value.items()}


def as_list(value: object) -> Sequence[object]:
    """Narrow a value to a list.

    Args:
        value: The value.

    Returns:
        A new list with the same items.
    """
    if not isinstance(value, list):
        fail("an array", value)
    return list(value)


def as_str(value: object) -> str:
    """Narrow a value to a string.

    Args:
        value: The value.

    Returns:
        The string.
    """
    if not isinstance(value, str):
        fail("a string", value)
    return value


def as_int(value: object) -> int:
    """Narrow a value to an integer (booleans are not integers here).

    Args:
        value: The value.

    Returns:
        The integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        fail("an integer", value)
    return value


def as_bool(value: object) -> bool:
    """Narrow a value to a boolean.

    Args:
        value: The value.

    Returns:
        The boolean.
    """
    if not isinstance(value, bool):
        fail("a boolean", value)
    return value


def get_str(obj: Mapping[str, object], key: str, default: str = "") -> str:
    """Read a string member.

    Args:
        obj: The object.
        key: The member name.
        default: The value when the member is missing or null.

    Returns:
        The string.
    """
    value = obj.get(key)
    return default if value is None else as_str(value)


def get_int(obj: Mapping[str, object], key: str) -> int:
    """Read an integer member (0 when missing or null).

    Args:
        obj: The object.
        key: The member name.

    Returns:
        The integer.
    """
    value = obj.get(key)
    return 0 if value is None else as_int(value)


def get_str_list(obj: Mapping[str, object], key: str) -> Sequence[str]:
    """Read a list-of-strings member (empty when missing or null).

    Args:
        obj: The object.
        key: The member name.

    Returns:
        The strings.
    """
    value = obj.get(key)
    return () if value is None else tuple(as_str(item) for item in as_list(value))


def get_objects(obj: Mapping[str, object], key: str) -> Sequence[Mapping[str, object]]:
    """Read a list-of-objects member (empty when missing or null).

    Args:
        obj: The object.
        key: The member name.

    Returns:
        The objects.
    """
    value = obj.get(key)
    return [] if value is None else [as_dict(item) for item in as_list(value)]


def loads(data: bytes | str) -> object:
    """Parse JSON.

    Args:
        data: The JSON text.

    Returns:
        The value.

    Raises:
        ShapeError: If the text is not JSON.
    """
    try:
        parsed: object = json.loads(data)
    except ValueError as error:
        raise ShapeError(f"not JSON: {error}") from None
    return parsed
