"""A pidge server in-process, backed by the in-memory store, for client tests."""

import contextlib
import dataclasses
import email.utils
import http.server
import pathlib
import threading
import unittest
from collections.abc import Iterator, Mapping, Sequence

import pidge.tests
from pidge.mcp import handle_rpc
from pidge.web import http_reply
from pidge.memstore import MemoryStore
from pidge.store import NotFound
from pidge.tests.fakes import World, deliver, make_world, new_cache, sample_raw
from pidge.tests.mcpclient import TOOLS
from pidge.tests.sessionkey import SESSION_KEY
from pidge.toolkit import Services

from .. import jsonshape
from ..transport import Reply, Session, Transport

TOKEN = "test-token"
ENDPOINT = "https://pidge.example/mcp"
PIDGE_FIXTURES = pathlib.Path(pidge.tests.__file__).parent / "fixtures"
HERE = pathlib.Path(__file__).parent
CLIENT_FIXTURES = HERE / "fixtures"
BASE = 1_767_225_600


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class InProcess:
    """A transport that hands requests straight to pidge's ``handle_rpc``.

    Attributes:
        services: What the tools run against.
        store: The in-memory bucket (presigned ``memory:`` URLs read from it).
        token: The only bearer token accepted.
    """

    services: Services
    store: MemoryStore
    token: str = TOKEN

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> Reply:
        """Serve a JSON-RPC POST.

        Args:
            url: The URL (ignored).
            headers: The request headers.
            body: The request body.

        Returns:
            401 for a wrong token, 202 for a notification, else the JSON-RPC
            response.
        """
        if headers.get("Authorization") != f"Bearer {self.token}":
            return Reply(status=401, body=b'{"error": "invalid_token"}')
        answer = handle_rpc(self.services, TOOLS, body)
        event = {"headers": {"accept": headers.get("Accept", "")}}
        response = http_reply(event, answer)
        return Reply(
            status=jsonshape.as_int(response["statusCode"]),
            body=jsonshape.as_str(response["body"]).encode(),
            headers={
                name: jsonshape.as_str(value)
                for name, value in jsonshape.as_dict(response["headers"]).items()
            },
        )

    def get(self, url: str) -> Reply:
        """Serve a presigned ``memory:`` URL from the store.

        Args:
            url: The URL.

        Returns:
            The object, or a 404.
        """
        key = url.removeprefix("memory:").partition("?")[0]
        try:
            return Reply(status=200, body=self.store.get(key).body)
        except NotFound:
            return Reply(status=404, body=b"NoSuchKey")


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Mailbox:
    """A fake deployment and a client session connected to it.

    Attributes:
        world: The fake environment.
        transport: The in-process transport.
        session: A session using it.
    """

    world: World
    transport: Transport
    session: Session


def empty_mailbox(case: unittest.TestCase) -> Mailbox:
    """Build an empty fake deployment.

    Args:
        case: The test case (for cleanup).

    Returns:
        The mailbox.
    """
    world = make_world(case)
    services = Services(
        context=world.context,
        cache=new_cache(world, "cache"),
        session_key=SESSION_KEY,
        endpoint=ENDPOINT,
    )
    transport = InProcess(services=services, store=world.store)
    session = Session(endpoint=ENDPOINT, token=TOKEN, transport=transport)
    return Mailbox(world=world, transport=transport, session=session)


def fixture_paths() -> Sequence[pathlib.Path]:
    """List the .eml fixtures: pidge's, then the client's.

    Returns:
        The paths.
    """
    return [
        *sorted(PIDGE_FIXTURES.glob("*.eml")),
        *sorted(CLIENT_FIXTURES.glob("*.eml")),
    ]


def receive(mailbox: Mailbox, name: str, raw: bytes) -> str:
    """Deliver a message a second after the last one.

    Args:
        mailbox: The mailbox.
        name: The SES object name.
        raw: The message.

    Returns:
        Its ID.
    """
    mailbox.world.clock.advance(1)
    return deliver(mailbox.world, name, raw)


def fixture_mailbox(case: unittest.TestCase) -> tuple[Mailbox, Mapping[str, str]]:
    """Build a mailbox holding every .eml fixture.

    Args:
        case: The test case.

    Returns:
        The mailbox, and message IDs by fixture name (without ``.eml``).
    """
    mailbox = empty_mailbox(case)
    ids = {
        path.stem: receive(mailbox, path.stem, path.read_bytes())
        for path in fixture_paths()
    }
    return mailbox, ids


def dated(subject: str, when: int) -> bytes:
    """Build a message with a given Date header.

    Args:
        subject: The subject.
        when: The date in epoch seconds.

    Returns:
        The raw message.
    """
    return sample_raw(subject, f"Body of {subject}", {}).replace(
        b"Thu, 01 Jan 2026 00:00:00 +0000",
        email.utils.formatdate(when, usegmt=True).encode(),
    )


class Handler(http.server.BaseHTTPRequestHandler):  # noqa: SLD401,SLD504
    """Serves a transport over real HTTP.

    Attributes:
        transport: The transport that answers.
    """

    transport: Transport

    def answer(self, reply: Reply) -> None:
        """Send a reply.

        Args:
            reply: The reply.
        """
        self.send_response(reply.status)
        for name, value in reply.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(reply.body)))
        self.end_headers()
        self.wfile.write(reply.body)

    def do_POST(self) -> None:  # noqa: N802
        """Serve a POST."""
        body = self.rfile.read(int(self.headers["Content-Length"]))
        headers = {
            name: self.headers.get(name, "") for name in ("Authorization", "Accept")
        }
        self.answer(self.transport.post(self.path, headers, body))

    def do_GET(self) -> None:  # noqa: N802
        """Serve a GET of ``/memory:<key>``."""
        self.answer(self.transport.get(self.path.removeprefix("/")))

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Stay quiet.

        Args:
            format: The format.
            *args: Its arguments.
        """


@contextlib.contextmanager
def http_server(transport: Transport) -> Iterator[str]:
    """Serve a transport over HTTP on localhost.

    Args:
        transport: The transport.

    Yields:
        The server's base URL.
    """
    handler = type("BoundHandler", (Handler,), {"transport": transport})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
