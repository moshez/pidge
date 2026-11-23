"""Assertion helpers that keep strict type checking happy."""

from collections.abc import Callable, Mapping

from hamcrest import assert_that, equal_to

from .. import jsonvalue


def failure(action: Callable[[], object]) -> Exception | None:
    """Run an action that should fail.

    Args:
        action: The action.

    Returns:
        What it raised, or None if it succeeded.
    """
    try:
        action()
    except Exception as error:
        return error
    return None


def includes(actual: object, expected: Mapping[str, object]) -> None:
    """Check that a JSON object has some members.

    Args:
        actual: The object.
        expected: The members it must have, with their values.
    """
    members = jsonvalue.as_dict(actual)
    assert_that({key: members.get(key) for key in expected}, equal_to(dict(expected)))
