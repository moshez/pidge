"""What MCP tools are made of."""

import dataclasses
from collections.abc import Callable, Mapping, Sequence

from .arguments import ToolError
from .ports import Context, ShardSource

__all__ = ["Run", "Services", "Tool", "ToolError", "schema"]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Services:
    """What the tools run against.

    Attributes:
        context: The flow context.
        cache: The local shard cache.
    """

    context: Context
    cache: ShardSource


Run = Callable[[Services, Mapping[str, object]], object]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Tool:
    """An MCP tool.

    Attributes:
        name: The tool name.
        description: What it does, for the model.
        schema: The JSON Schema of its arguments.
        run: The implementation.
    """

    name: str
    description: str
    schema: Mapping[str, object]
    run: Run


def schema(
    properties: Mapping[str, object], required: Sequence[str]
) -> Mapping[str, object]:
    """Build an object schema.

    Args:
        properties: The property schemas.
        required: The required property names.

    Returns:
        The JSON Schema.
    """
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


TEXT: Mapping[str, object] = {"type": "string"}
TEXT_LIST: Mapping[str, object] = {"type": "array", "items": TEXT}
MESSAGE_ID: Mapping[str, object] = {
    "type": "string",
    "description": "A message id (ULID) from search results.",
}
