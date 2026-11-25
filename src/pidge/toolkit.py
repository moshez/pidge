"""What MCP tools are made of."""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from .arguments import ToolError
from .ports import Context, ShardSource

__all__ = ["Build", "Run", "Secret", "Services", "Tool", "ToolError", "schema"]


class Secret(Protocol):
    """A secret value, such as the session-token signing key."""

    def value(self) -> bytes:
        """Return the secret.

        Returns:
            The secret bytes.
        """


def quiet(line: str) -> None:
    """Discard a log line.

    Args:
        line: The line.
    """


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Build:
    """Which deploy is running, and where to say so.

    Attributes:
        name: The deployed git SHA (``PIDGE_BUILD``), or ``dev``.
        log: Writes one log line.
    """

    name: str
    log: Callable[[str], None]


DEV_BUILD = Build(name="dev", log=quiet)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Services:
    """What the tools run against.

    Attributes:
        context: The flow context.
        cache: The local shard cache.
        session_key: Signs and checks session tokens.
        build: The running build, logged on each ``initialize``.
        tools: Every tool the server has (set per request).
        endpoint: The MCP endpoint URL of the current request (set per request).
    """

    context: Context
    cache: ShardSource
    session_key: Secret
    build: Build = DEV_BUILD
    tools: Sequence["Tool"] = ()
    endpoint: str = ""


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
