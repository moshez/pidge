"""The Lambda Function URL front end: OAuth metadata and the MCP endpoint."""

import base64
import dataclasses
import json
from collections.abc import Mapping, Sequence

from . import jsonvalue
from .auth import AuthError, ScopeError, BearerValidator
from .mcp import Answer, handle_rpc
from .refresh import LIST_CHANGED
from .toolkit import Services, Tool

MCP_PATH = "/mcp"
METADATA_PATH = "/.well-known/oauth-protected-resource"
SCOPE_NAME = "full"

Response = Mapping[str, object]


def json_response(status: int, payload: object, headers: Mapping[str, str]) -> Response:
    """Build a Function URL response with a JSON body.

    Args:
        status: The HTTP status.
        payload: The body.
        headers: Extra headers.

    Returns:
        The response.
    """
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **headers},
        "body": json.dumps(payload),
    }


def resource_url(event: Mapping[str, object]) -> str:
    """Compute this server's resource URL from the request.

    Args:
        event: The Function URL event.

    Returns:
        ``https://<function URL host>/mcp``.
    """
    context = jsonvalue.get_dict(event, "requestContext")
    return f"https://{jsonvalue.get_str(context, 'domainName')}{MCP_PATH}"


def metadata(event: Mapping[str, object], issuer: str) -> Response:
    """Serve the OAuth protected-resource metadata (RFC 9728).

    Args:
        event: The Function URL event.
        issuer: The authorization server (Cognito user pool) issuer.

    Returns:
        The response.
    """
    resource = resource_url(event)
    payload = {
        "resource": resource,
        "authorization_servers": [issuer],
        "scopes_supported": [f"{resource}/{SCOPE_NAME}"],
        "bearer_methods_supported": ["header"],
        "resource_name": "Pidge mailbox",
    }
    return json_response(200, payload, {})


def challenge(event: Mapping[str, object], status: int, error: str) -> Response:
    """Build a 401/403 response pointing the client at the metadata.

    Args:
        event: The Function URL event.
        status: 401 or 403.
        error: The OAuth error code, or empty for a missing token.

    Returns:
        The response.
    """
    resource = resource_url(event)
    host = resource.removesuffix(MCP_PATH)
    parts = [f'resource_metadata="{host}{METADATA_PATH}{MCP_PATH}"']
    if error:
        parts.append(f'error="{error}"')
    if status == 403:
        parts.append(f'scope="{resource}/{SCOPE_NAME}"')
    header = "Bearer " + ", ".join(parts)
    return json_response(
        status, {"error": error or "unauthorized"}, {"www-authenticate": header}
    )


def request_body(event: Mapping[str, object]) -> bytes:
    """Decode the request body.

    Args:
        event: The Function URL event.

    Returns:
        The body bytes.
    """
    body = jsonvalue.get_str(event, "body")
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8")


def bearer_token(event: Mapping[str, object]) -> str:
    """Extract the bearer token from the Authorization header.

    Args:
        event: The Function URL event.

    Returns:
        The token, or an empty string.
    """
    headers = jsonvalue.get_dict(event, "headers")
    scheme, _, token = jsonvalue.get_str(headers, "authorization").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def accepts_events(event: Mapping[str, object]) -> bool:
    """Tell whether the client accepts a ``text/event-stream`` reply.

    Args:
        event: The Function URL event.

    Returns:
        True if the Accept header lists ``text/event-stream``.
    """
    accept = jsonvalue.get_str(jsonvalue.get_dict(event, "headers"), "accept")
    return "text/event-stream" in accept.lower()


def event_stream(messages: Sequence[Mapping[str, object]]) -> Response:
    """Build an SSE reply carrying JSON-RPC messages, buffered in one body.

    Args:
        messages: The messages, one event each, in order.

    Returns:
        The response.
    """
    body = "".join(
        f"event: message\ndata: {json.dumps(message)}\n\n" for message in messages
    )
    headers = {"content-type": "text/event-stream", "x-accel-buffering": "no"}
    return {"statusCode": 200, "headers": headers, "body": body}


def http_reply(event: Mapping[str, object], answer: Answer) -> Response:
    """Turn a JSON-RPC answer into an HTTP response.

    Args:
        event: The Function URL event (for its Accept header).
        answer: The answer.

    Returns:
        202 for a notification; SSE for a re-announcement the client
        accepts; JSON otherwise.
    """
    if answer.response is None:
        return {"statusCode": 202, "headers": {}, "body": ""}
    if answer.announce and accepts_events(event):
        return event_stream([LIST_CHANGED, answer.response])
    return json_response(200, answer.response, {})


def serve_mcp(
    event: Mapping[str, object],
    services: Services,
    validator: BearerValidator,
    tools: Sequence[Tool],
) -> Response:
    """Authenticate and serve one MCP POST.

    Args:
        event: The Function URL event.
        services: The services.
        validator: Checks the bearer token.
        tools: The tools.

    Returns:
        The response.
    """
    token = bearer_token(event)
    if not token:
        return challenge(event, 401, "")
    resource = resource_url(event)
    try:
        validator.validate(token, resource, f"{resource}/{SCOPE_NAME}")
    except AuthError:
        return challenge(event, 401, "invalid_token")
    except ScopeError:
        return challenge(event, 403, "insufficient_scope")
    per_request = dataclasses.replace(services, endpoint=resource)
    return http_reply(event, handle_rpc(per_request, tools, request_body(event)))


def route(
    event: Mapping[str, object],
    services: Services,
    validator: BearerValidator,
    tools: Sequence[Tool],
) -> Response:
    """Route a Function URL request.

    Args:
        event: The Function URL event.
        services: The services.
        validator: Checks bearer tokens.
        tools: The tools.

    Returns:
        The response.
    """
    http = jsonvalue.as_dict(jsonvalue.as_dict(event["requestContext"])["http"])
    method = jsonvalue.get_str(http, "method").upper()
    path = jsonvalue.get_str(event, "rawPath", "/").rstrip("/") or "/"
    if path.startswith(METADATA_PATH):
        return metadata(event, services.context.settings.issuer)
    if path != MCP_PATH:
        return json_response(404, {"error": "not found"}, {})
    if method != "POST":
        return json_response(405, {"error": "use POST"}, {"allow": "POST"})
    return serve_mcp(event, services, validator, tools)
