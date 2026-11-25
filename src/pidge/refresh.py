"""``refresh_tools``: re-announce the tool list to clients that cache it.

claude.ai reads ``tools/list`` once per conversation, so a tool deployed
later is invisible until a new conversation starts. ``refresh_tools`` lists
every tool the server has, and its ``tools/call`` reply carries a
``notifications/tools/list_changed`` notification ahead of the result (see
``mcp.py`` and ``web.py``), which may prompt the client to list tools again.
"""

import re
from collections.abc import Mapping

from .toolkit import Services, Tool, schema

NAME = "refresh_tools"
LIST_CHANGED: Mapping[str, object] = {
    "jsonrpc": "2.0",
    "method": "notifications/tools/list_changed",
}
FIRST_SENTENCE = re.compile(r".*?[.!?](?=\s|$)", re.DOTALL)


def summary(description: str) -> str:
    """Take the first sentence of a tool description.

    Args:
        description: The description.

    Returns:
        Its first sentence, or all of it if it has no sentence end.
    """
    text = description.strip()
    match = FIRST_SENTENCE.match(text)
    return text if match is None else match.group(0)


def run_refresh_tools(services: Services, args: Mapping[str, object]) -> object:
    """List every tool the server has, and the build.

    Args:
        services: The services (``tools`` is set per request).
        args: The tool arguments (none).

    Returns:
        ``tools`` (each ``name`` and ``summary``) and ``build``.
    """
    tools = [
        {"name": tool.name, "summary": summary(tool.description)}
        for tool in services.tools
    ]
    return {"tools": tools, "build": services.build.name}


REFRESH_TOOLS = Tool(
    name=NAME,
    description=(
        "Re-announce this server's tools; call it if a tool you expected is "
        "missing. The result lists every tool the server has; if one isn't "
        "callable for you, tell the user to start a new conversation."
    ),
    schema=schema({}, []),
    run=run_refresh_tools,
)

REFRESH_TOOL_SET = (REFRESH_TOOLS,)
