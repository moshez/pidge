"""The Lambda entry point: one function, every flow."""

import dataclasses
import enum
import urllib.parse
from collections.abc import Mapping, Sequence

from . import jsonvalue
from .auth import BearerValidator
from .indexer import run_tracked
from .ingest import INBOUND, ingest_or_record
from .killswitch import KillSwitch, is_trip, trip
from .outbox import OUTBOX, activate
from .reconcile import reconcile
from .sesevents import confirm
from .toolkit import Services, Tool
from .web import route


class Action(enum.Enum):
    """Custom payloads, such as ``{"action": "index"}``.

    Attributes:
        INDEX: Run the batch indexer.
        RECONCILE: Run the reconciler.
    """

    INDEX = enum.auto()
    RECONCILE = enum.auto()


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class App:
    """Everything the handler needs.

    Attributes:
        services: The flow context and shard cache.
        validator: Checks bearer tokens.
        tools: The MCP tools.
        killswitch: The budget kill switch.
    """

    services: Services
    validator: BearerValidator
    tools: Sequence[Tool]
    killswitch: KillSwitch


def object_key(record: Mapping[str, object]) -> str:
    """Read the object key from an S3 event record.

    Args:
        record: The record.

    Returns:
        The decoded key.
    """
    s3 = jsonvalue.as_dict(record["s3"])
    key = jsonvalue.get_str(jsonvalue.as_dict(s3["object"]), "key")
    return urllib.parse.unquote_plus(key)


def handle_s3(app: App, event: Mapping[str, object]) -> object:
    """Handle S3 object-created notifications.

    Args:
        app: The application.
        event: The S3 event.

    Returns:
        The keys handled.
    """
    context = app.services.context
    handled = []
    for record in jsonvalue.as_list(event["Records"]):
        key = object_key(jsonvalue.as_dict(record))
        parts = key.split("/")
        if key.startswith(INBOUND):
            ingest_or_record(context, key)
        elif key.startswith(OUTBOX) and parts[-1] == "message.eml":
            activate(context, parts[1])
        handled.append(key)
    return handled


def run_action(app: App, event: Mapping[str, object]) -> object:
    """Handle a custom ``action`` payload.

    Args:
        app: The application.
        event: The payload.

    Returns:
        A summary of what was done.
    """
    context = app.services.context
    action = Action[jsonvalue.get_str(event, "action").upper()]
    if action is Action.INDEX:
        return dict(indexed=run_tracked(context))
    written = reconcile(context, lambda replayed: dispatch(app, replayed))
    return dict(ops_messages=list(written))


def dispatch(app: App, event: Mapping[str, object]) -> object:
    """Pick the flow from the shape of the event and run it.

    Args:
        app: The application.
        event: The Lambda event.

    Returns:
        The flow's result.

    Raises:
        ValueError: If the event has an unrecognized shape.
    """
    if is_trip(app.killswitch, event):
        return trip(app.killswitch)
    if "Records" in event:
        return handle_s3(app, event)
    if event.get("source") == "aws.ses":
        return confirm(app.services.context, event)
    if "requestContext" in event:
        return route(event, app.services, app.validator, app.tools)
    if "action" in event:
        return run_action(app, event)
    raise ValueError("unrecognized event")
