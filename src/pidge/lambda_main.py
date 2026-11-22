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
from .awsclients import LambdaInvoker, SesSender
from .handler import App, dispatch
from .ports import Context, Settings
from .s3store import S3Store
from .search import ShardCache
from .toolkit import Services
from .tools_read import READ_TOOLS
from .tools_write import WRITE_TOOLS

FETCH_TIMEOUT = 5


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


@functools.cache
def build_app() -> App:  # pragma: no cover
    """Build the application once per Lambda execution environment.

    Returns:
        The application.
    """
    environ = os.environ
    settings = settings_from(environ)
    workdir = tempfile.gettempdir()
    context = Context(
        store=S3Store.create(boto3.client("s3"), environ["BUCKET"]),
        sender=SesSender.create(boto3.client("sesv2"), settings.configuration_set),
        invoker=LambdaInvoker.create(
            boto3.client("lambda"), environ["AWS_LAMBDA_FUNCTION_NAME"]
        ),
        settings=settings,
        clock=time.time,
        new_id=lambda: ulid.new_ulid(time.time, random.SystemRandom().getrandbits),
        workdir=workdir,
    )
    services = Services(
        context=context, cache=ShardCache.create(os.path.join(workdir, "shards"))
    )
    validator = TokenValidator.create(settings.issuer, fetch_url)
    return App(
        services=services, validator=validator, tools=(*READ_TOOLS, *WRITE_TOOLS)
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
