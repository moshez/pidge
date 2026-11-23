"""Run the real Lambda image against moto, with AWS's glue emulated.

The pieces:

- **moto** (in its own container) stands in for S3 and SES.
- **The image under test** runs under the Lambda runtime interface emulator,
  with libfaketime preloaded so tests can move its clock forward.
- **An emulator on the host** sits between the two. It forwards every AWS
  call to moto, and does what AWS itself would do around the function:
  S3 event notifications for ``inbound/`` and ``outbox/*/message.eml``,
  Lambda's asynchronous invocations (two retries, then a failure record in
  the bucket, as the on-failure destination does), and capturing SES sends
  so tests can publish the matching EventBridge events. It also serves the
  JWKS for a fake Cognito issuer, and can inject AWS errors on chosen
  requests.

Tests talk to the function only through events, exactly as AWS would.
"""

import base64
import dataclasses
import datetime
import http.client
import http.server
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence

import boto3
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

REGION = "us-east-2"
BUCKET = "pidge-mail-e2e"
DOMAIN = "example.org"
FROM_ADDRESS = f"mail@{DOMAIN}"
OWNER = "owner@example.net"
FUNCTION_HOST = "abc123.lambda-url.us-east-2.on.aws"
RESOURCE = f"https://{FUNCTION_HOST}/mcp"
SCOPE = f"{RESOURCE}/full"
KILL_SWITCH_TOPIC = "arn:aws:sns:us-east-2:123456789012:budget-killswitch"
MOTO_IMAGE = os.environ.get("MOTO_IMAGE", "motoserver/moto:5.2.5")
CONTAINER_HOST = "host.docker.internal"
ASYNC_ATTEMPTS = 3  # Lambda's default: the first try plus two retries
NOTIFIED = re.compile(r"^(inbound/.+|outbox/[^/]+/message\.eml)$")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


def wait_until(check: Callable[[], bool], what: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while not check():
        if time.time() > deadline:
            raise TimeoutError(what)
        time.sleep(0.2)


def reachable(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=2).read()
    except OSError:
        return False
    return True


@dataclasses.dataclass
class Fault:
    """Fail matching AWS requests a number of times."""

    method: str
    pattern: str
    times: int
    status: int = 503
    code: str = "ServiceUnavailable"
    hits: int = 0

    def matches(self, method: str, path: str) -> bool:
        return (
            self.hits < self.times
            and method == self.method
            and re.search(self.pattern, urllib.parse.unquote(path)) is not None
        )


@dataclasses.dataclass
class SesSend:
    """One SendEmail call that moto accepted."""

    message_id: str
    raw: bytes
    recipients: Sequence[str]
    tags: Mapping[str, str]


class Emulator:
    """The AWS glue around the function (see the module docstring)."""

    def __init__(self, moto_port: int) -> None:
        self.moto_port = moto_port
        self.lock = threading.Lock()
        self.faults: list[Fault] = []
        self.queue: list[Mapping[str, object]] = []
        self.sends: list[SesSend] = []
        self.concurrency: dict[str, int] = {}
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), self.handler())
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def issuer(self) -> str:
        return f"http://{CONTAINER_HOST}:{self.port}/issuer"

    def reset(self) -> None:
        with self.lock:
            self.faults.clear()
            self.queue.clear()
            self.sends.clear()
            self.concurrency.clear()

    def fault(self, method: str, pattern: str, times: int, **kwargs: object) -> Fault:
        fault = Fault(method, pattern, times, **kwargs)  # type: ignore[arg-type]
        with self.lock:
            self.faults.append(fault)
        return fault

    def take_queue(self) -> list[Mapping[str, object]]:
        with self.lock:
            taken, self.queue[:] = list(self.queue), []
        return taken

    def jwks(self) -> bytes:
        public = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return json.dumps(
            {"keys": [dict(public, kid="e2e", alg="RS256", use="sig")]}
        ).encode()

    def token(self, now: float | None = None, **overrides: object) -> str:
        now = int(time.time() if now is None else now)
        claims: dict[str, object] = dict(
            iss=self.issuer,
            token_use="access",
            scope=SCOPE,
            client_id="e2e-client",
            sub="owner",
            iat=now,
            exp=now + 3600,
        )
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "e2e"})

    def handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        emulator = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def respond(
                self, status: int, body: bytes, headers: Mapping[str, str]
            ) -> None:
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_any(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if self.headers.get("Transfer-Encoding") == "chunked":
                    body = read_chunked(self.rfile)
                status, headers, payload = emulator.serve(
                    self.command, self.path, dict(self.headers.items()), body
                )
                self.respond(status, payload, headers)

            do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = handle_any

        return Handler

    def serve(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, Mapping[str, str], bytes]:
        if path == "/issuer/.well-known/jwks.json":
            return 200, {"Content-Type": "application/json"}, self.jwks()
        concurrency = re.match(r"^/2017-10-31/functions/([^/]+)/concurrency", path)
        if concurrency and method == "PUT":
            limit = int(json.loads(body)["ReservedConcurrentExecutions"])
            with self.lock:
                self.concurrency[urllib.parse.unquote(concurrency.group(1))] = limit
            reply = {"ReservedConcurrentExecutions": limit}
            return 200, {"Content-Type": "application/json"}, json.dumps(reply).encode()
        invoke = re.match(r"^/2015-03-31/functions/([^/]+)/invocations", path)
        if invoke:
            with self.lock:
                self.queue.append(json.loads(body))
            return 202, {"Content-Type": "application/json"}, b""
        with self.lock:
            fault = next((f for f in self.faults if f.matches(method, path)), None)
            if fault:
                fault.hits += 1
        if fault:
            error = f"<Error><Code>{fault.code}</Code><Message>injected by e2e</Message></Error>"
            if path.startswith("/v2/"):
                return (
                    fault.status,
                    {
                        "Content-Type": "application/json",
                        "x-amzn-ErrorType": fault.code,
                    },
                    json.dumps({"message": "injected by e2e"}).encode(),
                )
            return fault.status, {"Content-Type": "application/xml"}, error.encode()
        status, reply_headers, reply = self.forward(method, path, headers, body)
        if status < 300:
            self.after(method, path, body, reply)
        return status, reply_headers, reply

    def forward(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, Mapping[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.moto_port, timeout=60)
        skip = {"content-length", "transfer-encoding", "connection", "expect"}
        sent = {k: v for k, v in headers.items() if k.lower() not in skip}
        connection.request(method, path, body=body or None, headers=sent)
        response = connection.getresponse()
        reply = response.read()
        keep = {
            k: v
            for k, v in response.getheaders()
            if k.lower() not in ("content-length", "transfer-encoding", "connection")
        }
        connection.close()
        return response.status, keep, reply

    def after(self, method: str, path: str, body: bytes, reply: bytes) -> None:
        parsed = urllib.parse.urlparse(path)
        bucket, _, key = parsed.path.lstrip("/").partition("/")
        key = urllib.parse.unquote(key)
        if (
            method == "PUT"
            and bucket == BUCKET
            and NOTIFIED.match(key)
            and not parsed.query
        ):
            with self.lock:
                self.queue.append(s3_event(key))
        if method == "POST" and parsed.path == "/v2/email/outbound-emails":
            request = json.loads(body)
            raw = base64.b64decode(request["Content"]["Raw"]["Data"])
            tags = {t["Name"]: t["Value"] for t in request.get("EmailTags", [])}
            recipients = request.get("Destination", {}).get("ToAddresses", [])
            send = SesSend(json.loads(reply)["MessageId"], raw, recipients, tags)
            with self.lock:
                self.sends.append(send)


def read_chunked(stream: object) -> bytes:
    data = b""
    while True:
        size = int(getattr(stream, "readline")().split(b";")[0], 16)
        if size == 0:
            getattr(stream, "readline")()
            return data
        data += getattr(stream, "read")(size)
        getattr(stream, "readline")()


def s3_event(key: str) -> Mapping[str, object]:
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": BUCKET},
                    "object": {"key": urllib.parse.quote_plus(key)},
                },
            }
        ]
    }


def ses_event(kind: str, send: SesSend, **detail: object) -> Mapping[str, object]:
    """An SES event as EventBridge delivers it."""
    detail_type = {
        "Send": "Email Sent",
        "Delivery": "Email Delivered",
        "Bounce": "Email Bounced",
        "Reject": "Email Rejected",
    }[kind]
    mail = {
        "messageId": send.message_id,
        "destination": list(send.recipients),
        "tags": {name: [value] for name, value in send.tags.items()},
    }
    return {
        "source": "aws.ses",
        "detail-type": detail_type,
        "detail": dict(eventType=kind, mail=mail, **detail),
    }


@dataclasses.dataclass
class Invocation:
    """What one invocation returned."""

    payload: object
    error: str

    @property
    def ok(self) -> bool:
        return not self.error


class Stack:
    """moto, the emulator and the function, ready for one test."""

    def __init__(self, image: str) -> None:
        self.image = image
        self.workdir = pathlib.Path(tempfile.mkdtemp(prefix="pidge-e2e-"))
        self.moto_port = free_port()
        self.moto = docker(
            "run", "-d", "--rm", "-p", f"127.0.0.1:{self.moto_port}:5000", MOTO_IMAGE
        )
        self.moto_url = f"http://127.0.0.1:{self.moto_port}"
        wait_until(lambda: reachable(f"{self.moto_url}/moto-api/"), "moto to start")
        self.emulator = Emulator(self.moto_port)
        self.proxy_url = f"http://127.0.0.1:{self.emulator.port}"
        (self.workdir / "aws-config").write_text(
            "[default]\nregion = us-east-2\ns3 =\n    addressing_style = path\n"
        )
        (self.workdir / "faketime").mkdir()
        self.faketime_lib = find_faketime()
        self.function: str | None = None
        self.function_port = free_port()
        self.offset = 0

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self.stop_function()
        subprocess.run(["docker", "rm", "-f", self.moto], stdout=subprocess.DEVNULL)
        self.emulator.server.shutdown()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def reset(self) -> None:
        """Fresh AWS state, a fresh function and the real time."""
        self.stop_function()
        self.emulator.reset()
        urllib.request.urlopen(
            urllib.request.Request(f"{self.moto_url}/moto-api/reset", method="POST")
        ).read()
        self.set_offset(0)
        s3 = self.client("s3", direct=True)
        s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        ses = self.client("sesv2", direct=True)
        ses.create_email_identity(EmailIdentity=DOMAIN)
        ses.create_configuration_set(ConfigurationSetName="pidge")
        receiving = self.client("ses", direct=True)
        receiving.create_receipt_rule_set(RuleSetName="pidge")
        receiving.set_active_receipt_rule_set(RuleSetName="pidge")
        self.start_function()

    def start_function(self) -> None:
        env = dict(
            AWS_REGION=REGION,
            AWS_DEFAULT_REGION=REGION,
            AWS_ACCESS_KEY_ID="e2e",
            AWS_SECRET_ACCESS_KEY="e2e",
            AWS_ENDPOINT_URL=f"http://{CONTAINER_HOST}:{self.emulator.port}",
            AWS_CONFIG_FILE="/e2e/aws-config",
            AWS_MAX_ATTEMPTS="1",
            AWS_LAMBDA_FUNCTION_NAME="pidge",
            BUCKET=BUCKET,
            MAIL_DOMAIN=DOMAIN,
            FROM_ADDRESS=FROM_ADDRESS,
            OWNER_ADDRESS=OWNER,
            CONFIGURATION_SET="pidge",
            MESSAGE_ID_DOMAIN="us-east-2.amazonses.com",
            ISSUER=self.emulator.issuer,
            USER_POOL_ID="us-east-2_e2e",
            KILL_SWITCH_TOPIC_ARN=KILL_SWITCH_TOPIC,
            LD_PRELOAD="/e2e/libfaketime.so.1",
            FAKETIME_TIMESTAMP_FILE="/e2e/faketime/now",
            FAKETIME_NO_CACHE="1",
        )
        flags = [
            arg for name, value in env.items() for arg in ("-e", f"{name}={value}")
        ]
        self.function = docker(
            "run",
            "-d",
            "--rm",
            "-p",
            f"127.0.0.1:{self.function_port}:8080",
            "--add-host",
            f"{CONTAINER_HOST}:host-gateway",
            "-v",
            f"{self.workdir}:/e2e",
            "-v",
            f"{self.faketime_lib}:/e2e/libfaketime.so.1:ro",
            *flags,
            self.image,
        )
        wait_until(self.function_ready, "the function to start")

    def stop_function(self) -> None:
        if self.function:
            subprocess.run(
                ["docker", "rm", "-f", self.function], stdout=subprocess.DEVNULL
            )
            self.function = None

    def function_ready(self) -> bool:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.function_port, timeout=2
        )
        try:
            connection.request("GET", "/")
            connection.getresponse().read()
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()
        return True

    def logs(self) -> str:
        if not self.function:
            return ""
        return subprocess.run(
            ["docker", "logs", self.function], text=True, capture_output=True
        ).stdout[-4000:]

    # -- time ------------------------------------------------------------------

    def set_offset(self, seconds: int) -> None:
        self.offset = seconds
        (self.workdir / "faketime" / "now").write_text(f"+{seconds}s\n")

    def advance(self, seconds: int) -> None:
        """Move the function's clock forward."""
        self.set_offset(self.offset + seconds)

    def now(self) -> float:
        return time.time() + self.offset

    # -- AWS -------------------------------------------------------------------

    def client(self, service: str, direct: bool = False):  # type: ignore[no-untyped-def]
        """A boto3 client for moto, through the emulator unless ``direct``."""
        return boto3.client(
            service,
            region_name=REGION,
            endpoint_url=self.moto_url if direct else self.proxy_url,
            aws_access_key_id="e2e",
            aws_secret_access_key="e2e",
            config=boto3.session.Config(s3={"addressing_style": "path"}),
        )

    def keys(self, prefix: str = "") -> list[str]:
        s3 = self.client("s3", direct=True)
        found: list[str] = []
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=BUCKET, Prefix=prefix
        ):
            found.extend(item["Key"] for item in page.get("Contents", []))
        return found

    def read(self, key: str) -> bytes:
        s3 = self.client("s3", direct=True)
        return bytes(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())

    def read_json(self, key: str) -> Mapping[str, object]:
        loaded: Mapping[str, object] = json.loads(self.read(key))
        return loaded

    def receive(self, raw: bytes, notify: bool = True) -> str:
        """Do what SES receiving does: write the message under ``inbound/``."""
        ses_id = uuid.uuid4().hex
        s3 = self.client("s3", direct=not notify)
        s3.put_object(Bucket=BUCKET, Key=f"inbound/{ses_id}", Body=raw)
        return ses_id

    # -- Lambda ----------------------------------------------------------------

    def invoke_once(self, event: Mapping[str, object]) -> Invocation:
        if self.emulator.concurrency.get("pidge") == 0:
            # What Lambda does with reserved concurrency 0: throttle everything.
            return Invocation(None, "TooManyRequestsException: Rate Exceeded.")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.function_port}/2015-03-31/functions/function/invocations",
            data=json.dumps(event).encode(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read() or b"null")
        if isinstance(payload, dict) and "errorType" in payload:
            return Invocation(
                payload, f"{payload['errorType']}: {payload.get('errorMessage')}"
            )
        return Invocation(payload, "")

    def invoke(self, event: Mapping[str, object]) -> Invocation:
        """A synchronous invocation (Function URL)."""
        return self.invoke_once(event)

    def invoke_async(self, event: Mapping[str, object]) -> Invocation:
        """An asynchronous invocation, with Lambda's retries and failure record."""
        result = Invocation(None, "")
        for attempt in range(1, ASYNC_ATTEMPTS + 1):
            result = self.invoke_once(event)
            if result.ok:
                return result
        self.record_failure(event, result)
        return result

    def record_failure(self, event: Mapping[str, object], result: Invocation) -> None:
        """What Lambda's S3 on-failure destination writes."""
        now = datetime.datetime.now(datetime.UTC)
        record = {
            "version": "1.0",
            "timestamp": now.isoformat(),
            "requestContext": {
                "requestId": str(uuid.uuid4()),
                "functionArn": f"arn:aws:lambda:{REGION}:123456789012:function:pidge",
                "condition": "RetriesExhausted",
                "approximateInvokeCount": ASYNC_ATTEMPTS,
            },
            "requestPayload": event,
            "responseContext": {"statusCode": 200, "functionError": "Unhandled"},
            "responsePayload": result.payload,
        }
        key = f"aws/lambda/async/pidge/{now:%Y/%m/%d/%H}/{uuid.uuid4()}"
        self.client("s3", direct=True).put_object(
            Bucket=BUCKET, Key=key, Body=json.dumps(record).encode()
        )

    def settle(self, limit: int = 500) -> list[Invocation]:
        """Deliver queued notifications and async invocations until none are left."""
        results = []
        for _ in range(limit):
            queued = self.emulator.take_queue()
            if not queued:
                return results
            for event in queued:
                results.append(self.invoke_async(event))
        raise RuntimeError("events kept coming")

    def reconcile(self) -> Invocation:
        result = self.invoke_async({"action": "reconcile"})
        self.settle()
        return result

    def index(self) -> Invocation:
        result = self.invoke_async({"action": "index"})
        self.settle()
        return result

    def notify(self, topic: str, message: str) -> Invocation:
        """Deliver an SNS notification (SNS invokes Lambda asynchronously)."""
        record = {
            "EventSource": "aws:sns",
            "EventVersion": "1.0",
            "EventSubscriptionArn": f"{topic}:{uuid.uuid4()}",
            "Sns": {
                "Type": "Notification",
                "MessageId": str(uuid.uuid4()),
                "TopicArn": topic,
                "Subject": "AWS Budgets: pidge-monthly has exceeded your alert threshold",
                "Message": message,
            },
        }
        return self.invoke_async({"Records": [record]})

    def publish(self, kind: str, send: SesSend, **detail: object) -> Invocation:
        """Publish an SES event through EventBridge."""
        result = self.invoke_async(ses_event(kind, send, **detail))
        self.settle()
        return result

    # -- the Function URL --------------------------------------------------------

    def http(
        self,
        method: str,
        path: str,
        body: object = None,
        token: str | None = None,
    ) -> tuple[int, Mapping[str, str], object]:
        headers = {"content-type": "application/json"}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        event = {
            "version": "2.0",
            "rawPath": path,
            "headers": headers,
            "body": "" if body is None else json.dumps(body),
            "isBase64Encoded": False,
            "requestContext": {
                "domainName": FUNCTION_HOST,
                "http": {"method": method, "path": path},
            },
        }
        result = self.invoke(event)
        if not result.ok:
            raise AssertionError(result.error)
        response = result.payload
        assert isinstance(response, dict)
        text = response.get("body") or ""
        return (
            int(response["statusCode"]),
            response.get("headers", {}),
            json.loads(text) if text else None,
        )

    def rpc(
        self, method: str, params: object = None, token: str | None = None
    ) -> object:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        status, _, reply = self.http(
            "POST",
            "/mcp",
            body,
            self.emulator.token(self.now()) if token is None else token,
        )
        if status != 200 or not isinstance(reply, dict) or "result" not in reply:
            raise AssertionError(f"{method}: {status} {reply}")
        return reply["result"]

    def tool(self, tool: str, /, **arguments: object) -> object:
        """Call an MCP tool and return its structured result."""
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        assert isinstance(result, dict)
        if result.get("isError"):
            raise ToolFailed(result["content"][0]["text"])
        self.settle()
        return result["structuredContent"]


class ToolFailed(Exception):
    """An MCP tool returned an error result."""


def find_faketime() -> str:
    """Locate the libfaketime shared object shipped in the libfaketime wheel."""
    import libfaketime  # noqa: PLC0415

    root = pathlib.Path(libfaketime.__file__).parent
    found = sorted(root.rglob("libfaketime.so.1"))
    if not found:
        raise RuntimeError("libfaketime.so.1 not found in the libfaketime package")
    return str(found[0])
