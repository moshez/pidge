"""pidge_client needs nothing but the standard library."""

import ast
import pathlib
import sys
import unittest
from collections.abc import Set as AbstractSet

from hamcrest import assert_that, equal_to

PACKAGE = pathlib.Path(__file__).parents[1]


def imported(path: pathlib.Path) -> AbstractSet[str]:
    """List the top-level modules a file imports absolutely.

    Args:
        path: The Python file.

    Returns:
        The module names.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.partition(".")[0])
    return found


class TestStandardLibraryOnly(unittest.TestCase):
    """Usable from a clone with only PYTHONPATH set."""

    def test_imports(self) -> None:
        """Outside the tests, every import is the standard library or relative."""
        modules: set[str] = set()
        for path in PACKAGE.glob("*.py"):
            modules |= imported(path)
        assert_that(modules - sys.stdlib_module_names, equal_to(set()))
