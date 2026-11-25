"""Server-sent events: how pidge streams a ``refresh_tools`` reply."""

from collections.abc import Mapping, Sequence

from . import jsonshape


def stream_messages(body: str) -> Sequence[Mapping[str, object]]:
    """Parse a server-sent-events body into its JSON-RPC messages.

    Args:
        body: The body.

    Returns:
        Each event's data, decoded, in order.
    """
    messages = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data = [
            line.removeprefix("data:").removeprefix(" ")
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        if data:
            messages.append(jsonshape.as_dict(jsonshape.loads("\n".join(data))))
    return messages
