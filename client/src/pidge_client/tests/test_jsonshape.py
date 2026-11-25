"""Narrowing JSON values: every wrong shape is a ShapeError."""

import unittest
from collections.abc import Callable

from hamcrest import assert_that, contains_string

from .. import jsonshape


class TestShapes(unittest.TestCase):
    """Wrong shapes and bad JSON are refused, naming what was expected."""

    def test_wrong_shapes(self) -> None:
        """Each narrowing names the shape it expected."""
        cases: dict[str, Callable[[], object]] = {
            "an array": lambda: jsonshape.as_list({}),
            "a string": lambda: jsonshape.as_str(1),
            "an integer": lambda: jsonshape.as_int(True),
            "a boolean": lambda: jsonshape.as_bool("yes"),
        }
        for expected, narrow in cases.items():
            with self.subTest(expected=expected):
                with self.assertRaises(jsonshape.ShapeError) as caught:
                    narrow()
                assert_that(str(caught.exception), contains_string(expected))

    def test_not_json(self) -> None:
        """Text that is not JSON is a ShapeError too."""
        with self.assertRaises(jsonshape.ShapeError):
            jsonshape.loads(b"{")
