"""The hard-coded signatures must match the server's ``tools/list``."""

import unittest
from collections.abc import Mapping

from hamcrest import assert_that, equal_to

from .. import jsonshape
from ..calls import SIGNATURES
from ..transport import list_tools
from .mailbox import empty_mailbox


def json_type(schema: Mapping[str, object]) -> str:
    """Describe a parameter schema the way signatures do.

    Args:
        schema: The JSON Schema of one parameter.

    Returns:
        ``string``, ``integer``, ``boolean``, or an item type plus ``[]``.
    """
    kind = jsonshape.get_str(schema, "type")
    if kind != "array":
        return kind
    return json_type(jsonshape.as_dict(schema["items"])) + "[]"


def server_signatures() -> Mapping[str, Mapping[str, str]]:
    """Read every tool's parameters from a live in-process server.

    Returns:
        Parameter types by name, by tool name.
    """
    case = unittest.TestCase()
    listed = list_tools(empty_mailbox(case).session)
    case.doCleanups()
    found = {}
    for tool in listed:
        properties = jsonshape.as_dict(
            jsonshape.as_dict(tool["inputSchema"])["properties"]
        )
        found[jsonshape.get_str(tool, "name")] = {
            name: json_type(jsonshape.as_dict(schema))
            for name, schema in properties.items()
        }
    return found


class TestSchemaDrift(unittest.TestCase):
    """Client and server agree on every tool and parameter."""

    def test_same_tools_and_parameters(self) -> None:
        """Tool names, parameter names and parameter types all match."""
        client = {signature.tool: dict(signature.params) for signature in SIGNATURES}
        assert_that(client, equal_to(server_signatures()))
