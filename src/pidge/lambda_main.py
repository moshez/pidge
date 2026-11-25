"""Production wiring: build the application from the environment."""

import functools
import os
import random
import tempfile
import time
import urllib.request
from collections.abc import Mapping

import boto3  # type: ignore[import-untyped]

from . import ulid
from .auth import TokenValidator
from .clients import CognitoPoolClients
from .awsclients import (
    LambdaInvoker,
    LambdaThrottle,
    SesReceiptRules,
    SesSender,
    SsmSecret,
)
from .handler import App, dispatch
from .killswitch import KillSwitch
from .ports import Context, Settings
from .s3store import S3Store
from .search import ShardCache
from .sessions import SESSION_TOOLS, EitherValidator, SessionValidator
from .toolkit import Build, Secret, Services
from .tools_read import READ_TOOLS
from .tools_write import WRITE_TOOLS

FETCH_TIMEOUT = 5
TOOLS = (*READ_TOOLS, *WRITE_TOOLS, *SESSION_TOOLS)


def fetch_url(url: str) -> bytes:  # pragma: no cover
    """Retrieve a URL's body (used for the Cognito JWKS).

    Args:
        url: The URL.

    Returns:
        The body.
    """
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
        body: bytes = response.read()
        return body


def settings_from(environ: Mapping[str, str]) -> Settings:  # pragma: no cover
    """Read the deployment settings.

    Args:
        environ: The process environment.

    Returns:
        The settings.
    """
    return Settings(
        mail_domain=environ["MAIL_DOMAIN"],
        from_address=environ["FROM_ADDRESS"],
        owner_address=environ["OWNER_ADDRESS"],
        configuration_set=environ["CONFIGURATION_SET"],
        message_id_domain=environ["MESSAGE_ID_DOMAIN"],
        issuer=environ["ISSUER"],
    )


def killswitch_from(environ: Mapping[str, str]) -> KillSwitch:  # pragma: no cover
    """Wire the budget kill switch.

    Args:
        environ: The process environment.

    Returns:
        The kill switch (it never trips if KILL_SWITCH_TOPIC_ARN is unset).
    """
    return KillSwitch(
        topic_arn=environ.get("KILL_SWITCH_TOPIC_ARN", ""),
        receipt_rules=SesReceiptRules.create(boto3.client("ses")),
        throttle=LambdaThrottle.create(
            boto3.client("lambda"), environ["AWS_LAMBDA_FUNCTION_NAME"]
        ),
        log=functools.partial(print, flush=True),
    )


def context_from(environ: Mapping[str, str]) -> Context:  # pragma: no cover
    """Wire the flow context to AWS.

    Args:
        environ: The process environment.

    Returns:
        The context.
    """
    settings = settings_from(environ)
    return Context(
        store=S3Store.create(boto3.client("s3"), environ["BUCKET"]),
        sender=SesSender.create(boto3.client("sesv2"), settings.configuration_set),
        invoker=LambdaInvoker.create(
            boto3.client("lambda"), environ["AWS_LAMBDA_FUNCTION_NAME"]
        ),
        settings=settings,
        clock=time.time,
        new_id=lambda: ulid.new_ulid(time.time, random.SystemRandom().getrandbits),
        workdir=tempfile.gettempdir(),
    )


def build_from(environ: Mapping[str, str]) -> Build:  # pragma: no cover
    """Name the running build.

    Args:
        environ: The process environment; Terraform sets PIDGE_BUILD to the
            deployed git SHA.

    Returns:
        The build (``dev`` when PIDGE_BUILD is unset), logging to stdout.
    """
    return Build(
        name=environ.get("PIDGE_BUILD", "dev"), log=functools.partial(print, flush=True)
    )


def services_from(
    context: Context, session_key: Secret
) -> Services:  # pragma: no cover
    """Wire what the MCP tools run against.

    Args:
        context: The flow context.
        session_key: Signs session tokens.

    Returns:
        The services.
    """
    return Services(
        context=context,
        cache=ShardCache.create(os.path.join(context.workdir, "shards")),
        session_key=session_key,
        build=build_from(os.environ),
    )


@functools.cache
def build_app() -> App:  # pragma: no cover
    """Build the application once per Lambda execution environment.

    Returns:
        The application.
    """
    context = context_from(os.environ)
    session_key = SsmSecret.create(
        boto3.client("ssm"), os.environ["SESSION_KEY_PARAMETER"]
    )
    return App(
        services=services_from(context, session_key),
        validator=EitherValidator.create(
            sessions=SessionValidator.create(session_key, context.clock),
            cognito=TokenValidator.create(context.settings.issuer, fetch_url),
        ),
        tools=TOOLS,
        killswitch=killswitch_from(os.environ),
        pool=CognitoPoolClients.create(
            boto3.client("cognito-idp"), os.environ["USER_POOL_ID"]
        ),
    )


def lambda_handler(
    event: Mapping[str, object], _context: object
) -> object:  # pragma: no cover
    """The Lambda entry point.

    Args:
        event: The Lambda event.
        _context: The Lambda context (unused).

    Returns:
        The flow's result.
    """
    return dispatch(build_app(), event)
