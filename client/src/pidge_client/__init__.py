"""A stdlib-only client for pidge's MCP endpoint, plus HTML views of mail.

Layers, each usable on its own:

- :mod:`pidge_client.transport`: :class:`~pidge_client.transport.Session`
  sends JSON-RPC ``tools/call`` requests.
- :mod:`pidge_client.calls`: one typed wrapper per tool.
- :mod:`pidge_client.workflows`: paging, body download and archiving.
- :mod:`pidge_client.views`: pure HTML renderers (no network).
"""

from .calls import Draft, SearchArgs
from .transport import AuthError, RpcError, Session, ToolError
from .workflows import iter_search

__all__ = [
    "AuthError",
    "Draft",
    "RpcError",
    "SearchArgs",
    "Session",
    "ToolError",
    "iter_search",
]
