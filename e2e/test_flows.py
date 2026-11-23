"""End-to-end tests: the real Lambda image, moto, and AWS's glue emulated.

Run with ``nox -s e2e`` (needs Docker). ``PIDGE_IMAGE`` names the image under
test (default ``pidge:candidate``).
"""

import email
import email.policy
import os
import pathlib
import re
import tempfile
import unittest
import urllib.parse
import urllib.request
from collections.abc import Mapping
from email.message import EmailMessage

from hamcrest import (
    assert_that,
    contains_string,
    empty,
    equal_to,
    has_item,
    has_length,
    is_not,
    starts_with,
)

from .harness import (
    BUCKET,
    CONTAINER_HOST,
    DOMAIN,
    FROM_ADDRESS,
    FUNCTION_HOST,
    KILL_SWITCH_TOPIC,
    SCOPE,
    Stack,
    ToolFailed,
)

STACK: Stack

FIXTURES = pathlib.Path(__file__).parent.parent / "src/pidge/tests/fixtures"


def setUpModule() -> None:  # noqa: N802
    global STACK
    STACK = Stack(os.environ.get("PIDGE_IMAGE", "pidge:candidate"))


def tearDownModule() -> None:  # noqa: N802
    STACK.close()


def message(
    subject: str,
    body: str = "Hello there.",
    sender: str = "Alice <alice@example.com>",
    attachments: Mapping[str, bytes] | None = None,
    message_id: str = "",
) -> bytes:
    built = EmailMessage()
    built["From"] = sender
    built["To"] = f"Me <me@{DOMAIN}>"
    built["Subject"] = subject
    built["Date"] = "Thu, 01 Oct 2026 12:00:00 +0000"
    built["Message-ID"] = message_id or f"<{abs(hash(subject))}@example.com>"
    built.set_content(body)
    for name, data in (attachments or {}).items():
        built.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return built.as_bytes()


class EndToEnd(unittest.TestCase):
    """Each test starts from an empty bucket and a fresh function."""

    def setUp(self) -> None:
        STACK.reset()
        self.stack = STACK

    def tearDown(self) -> None:
        if any(error for _, error in getattr(self._outcome, "errors", []) if error):
            print(self.stack.logs())  # pragma: no cover

    # helpers -------------------------------------------------------------------

    def deliver(self, raw: bytes) -> str:
        """Receive a message and return its ULID."""
        ses_id = self.stack.receive(raw)
        results = self.stack.settle()
        assert_that([r.error for r in results if not r.ok], empty())
        return self.stack.read(f"inbound-seen/{ses_id}").decode()

    def meta(self, ulid: str) -> Mapping[str, object]:
        return self.stack.read_json(f"msgs/{ulid}/meta.json")

    def tags(self, ulid: str) -> list[str]:
        return sorted(self.meta(ulid)["tags"])  # type: ignore[arg-type]

    def search(self, **arguments: object) -> list[Mapping[str, object]]:
        found = self.stack.tool("search", **arguments)
        assert isinstance(found, dict)
        return found["results"]  # type: ignore[no-any-return]

    def ops(self) -> list[Mapping[str, object]]:
        return self.search(tags=["ops"])

    def send(self, **arguments: object) -> str:
        queued = self.stack.tool("send_email", **arguments)
        assert isinstance(queued, dict)
        return str(queued["outbox_id"])

    def outbox(self, outbox_id: str) -> Mapping[str, object]:
        return self.stack.read_json(f"outbox/{outbox_id}/state.json")


class TestFunctionUrl(EndToEnd):
    """OAuth resource-server behaviour at the Function URL."""

    def test_metadata(self) -> None:
        """Both metadata paths describe the resource and the issuer."""
        for path in [
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ]:
            status, _, body = self.stack.http("GET", path)
            assert_that(status, equal_to(200))
            assert isinstance(body, dict)
            assert_that(body["resource"], equal_to(f"https://{FUNCTION_HOST}/mcp"))
            assert_that(body["scopes_supported"], equal_to([SCOPE]))
            assert_that(body["authorization_servers"], has_item(starts_with("http")))

    def test_rejections(self) -> None:
        """Missing, forged, mis-addressed and under-scoped tokens are refused."""
        emulator = self.stack.emulator
        ping = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        cases = {
            "missing": (None, 401),
            "garbage": ("not-a-jwt", 401),
            "wrong issuer": (emulator.token(iss="https://evil.example"), 401),
            "id token": (emulator.token(token_use="id"), 401),
            "expired": (emulator.token(exp=1), 401),
            "wrong audience": (emulator.token(aud="https://other/mcp"), 401),
            "no scope": (emulator.token(scope="openid"), 403),
        }
        for name, (token, expected) in cases.items():
            with self.subTest(name):
                status, headers, _ = self.stack.http("POST", "/mcp", ping, token)
                assert_that(status, equal_to(expected))
                assert_that(
                    headers["www-authenticate"], contains_string("resource_metadata=")
                )

    def test_routes(self) -> None:
        """Unknown paths are 404 and GET on the endpoint is 405."""
        token = self.stack.emulator.token()
        assert_that(self.stack.http("GET", "/nope", None, token)[0], equal_to(404))
        assert_that(self.stack.http("GET", "/mcp", None, token)[0], equal_to(405))

    def test_protocol(self) -> None:
        """initialize, tools/list, notifications and errors follow JSON-RPC."""
        initialized = self.stack.rpc("initialize", {"protocolVersion": "2025-06-18"})
        assert isinstance(initialized, dict)
        assert_that(initialized["serverInfo"]["name"], equal_to("pidge"))
        listed = self.stack.rpc("tools/list")
        assert isinstance(listed, dict)
        names = sorted(tool["name"] for tool in listed["tools"])
        assert_that(
            names,
            equal_to(
                [
                    "get_attachment",
                    "get_message",
                    "list_tags",
                    "search",
                    "send_email",
                    "tag_email",
                ]
            ),
        )
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        status, _, _ = self.stack.http(
            "POST", "/mcp", note, self.stack.emulator.token()
        )
        assert_that(status, equal_to(202))
        bad = {"jsonrpc": "2.0", "id": 2, "method": "no/such"}
        _, _, reply = self.stack.http("POST", "/mcp", bad, self.stack.emulator.token())
        assert isinstance(reply, dict)
        assert_that(reply["error"]["code"], equal_to(-32601))


class TestInbound(EndToEnd):
    """SES receiving through to the mailbox tools."""

    def test_receive_read_and_download(self) -> None:
        """A received message is searchable, readable and its attachment downloads."""
        ulid = self.deliver(
            message(
                "Quarterly report",
                "Numbers inside.",
                attachments={"report.pdf": b"%PDF-1.7 data"},
            )
        )
        assert_that(self.tags(ulid), equal_to(["inbox"]))
        hits = self.search(query="quarterly")
        assert_that([hit["id"] for hit in hits], equal_to([ulid]))
        assert_that(hits[0]["ranked"], equal_to(False))
        read = self.stack.tool("get_message", id=ulid)
        assert isinstance(read, dict)
        assert_that(read["headers"]["subject"], equal_to("Quarterly report"))
        assert_that(read["body"], contains_string("Numbers inside."))
        name = read["headers"]["attachments"][0]["name"]
        assert_that(self.download(ulid, name), equal_to(b"%PDF-1.7 data"))
        assert_that(self.stack.tool("list_tags"), equal_to({"tags": {"inbox": 1}}))

    def download(self, ulid: str, name: str) -> bytes:
        """Fetch an attachment through get_attachment's presigned link."""
        link = self.stack.tool("get_attachment", id=ulid, name=name)
        assert isinstance(link, dict)
        url = urllib.parse.urlparse(link["url"])
        assert_that(url.hostname, equal_to(CONTAINER_HOST))
        local = url._replace(netloc=f"127.0.0.1:{self.stack.emulator.port}").geturl()
        data: bytes = urllib.request.urlopen(local).read()
        return data

    def test_html_body_renders_from_local_files(self) -> None:
        """0-body.html and the files it references download into one directory."""
        raw = (FIXTURES / "related.eml").read_bytes()
        ulid = self.deliver(raw)
        read = self.stack.tool("get_message", id=ulid)
        assert isinstance(read, dict)
        listed = [entry["name"] for entry in read["headers"]["attachments"]]
        assert_that(listed[0], equal_to("0-body.html"))
        html = self.download(ulid, "0-body.html").decode("utf-8")
        referenced = set(re.findall(r'(?:src|background)="([^"]+)"', html))
        assert_that(
            referenced, equal_to({"2-logo.png", "3-inline-1.jpg", "cid:missing@x"})
        )
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            for name in referenced - {"cid:missing@x"}:
                (folder / name).write_bytes(self.download(ulid, name))
            assert_that((folder / "2-logo.png").read_bytes()[:4], equal_to(b"\x89PNG"))
        assert_that(self.search(has_attachments=True), has_length(1))

    def test_setup_notification(self) -> None:
        """SES's setup notification is acknowledged and not stored as mail."""
        self.stack.client("s3").put_object(
            Bucket=BUCKET, Key="inbound/AMAZON_SES_SETUP_NOTIFICATION", Body=b"x"
        )
        self.stack.settle()
        assert_that(self.stack.keys("msgs/"), empty())
        assert_that(self.stack.keys("inbound-done/"), has_length(1))

    def test_unknown_charset(self) -> None:
        """A body in a charset Python lacks is still ingested."""
        raw = (
            b"From: a@example.com\r\nSubject: odd charset\r\n"
            b"Content-Type: text/plain; charset=x-unknown\r\n\r\nhello \xe9\r\n"
        )
        ulid = self.deliver(raw)
        assert_that(self.tags(ulid), equal_to(["inbox"]))

    def test_transient_failure_retries_with_same_id(self) -> None:
        """S3 failing once: Lambda's retry succeeds and keeps the first ULID."""
        self.stack.emulator.fault("PUT", r"/msgs/.*/raw\.eml$", times=1)
        ses_id = self.stack.receive(message("Flaky"))
        results = self.stack.settle()
        assert_that([r.ok for r in results], equal_to([True]))
        ulid = self.stack.read(f"inbound-seen/{ses_id}").decode()
        assert_that(
            {key.split("/")[1] for key in self.stack.keys("msgs/")}, equal_to({ulid})
        )
        assert_that(self.stack.keys("aws/"), empty())

    def test_poison_message_is_quarantined(self) -> None:
        """Five failed attempts store the raw message tagged ingest-failed, plus an ops note."""
        raw = message("Poison")
        self.stack.emulator.fault("PUT", r"/msgs/.*/raw\.eml$", times=5)
        ses_id = self.stack.receive(raw)
        self.stack.settle()  # three attempts, then the failure record
        assert_that(self.stack.keys("aws/lambda/async/"), has_length(1))
        assert_that(self.stack.keys("msgs/"), empty())
        self.stack.reconcile()  # the fourth attempt fails again
        self.stack.reconcile()  # the fifth is the last: quarantine
        ulid = self.stack.read(f"inbound-seen/{ses_id}").decode()
        assert_that(self.tags(ulid), equal_to(["inbox", "ingest-failed"]))
        assert_that(self.stack.read(f"msgs/{ulid}/raw.eml"), equal_to(raw))
        assert_that(self.stack.keys("aws/"), empty())
        notes = self.ops()
        assert_that(notes, has_length(1))
        assert_that(notes[0]["subject"], contains_string("could not be parsed"))

    def test_lost_notification_is_swept(self) -> None:
        """A message whose notification never came is ingested by the reconciler."""
        ses_id = self.stack.receive(message("Lost"), notify=False)
        self.stack.reconcile()
        assert_that(self.stack.keys("msgs/"), empty())  # too young to sweep yet
        self.stack.advance(16 * 60)
        self.stack.reconcile()
        ulid = self.stack.read(f"inbound-seen/{ses_id}").decode()
        assert_that(self.tags(ulid), equal_to(["inbox"]))


class TestOutbound(EndToEnd):
    """send_email through SES and its events."""

    def test_send_and_deliver(self) -> None:
        """Queued, handed to SES, marked sent with SES's Message-ID, delivered."""
        outbox_id = self.send(to=["bob@example.net"], subject="Hi Bob", body="See you.")
        state = self.outbox(outbox_id)
        assert_that(state["status"], equal_to("sending"))
        sends = self.stack.emulator.sends
        assert_that(sends, has_length(1))
        assert_that(sends[0].tags, equal_to({"outbox_id": outbox_id}))
        assert_that(sends[0].recipients, equal_to(["bob@example.net"]))
        sent = email.message_from_bytes(sends[0].raw, policy=email.policy.default)
        assert_that(str(sent["From"]), contains_string(FROM_ADDRESS))
        assert_that(self.tags(outbox_id), equal_to(["outbox"]))
        self.stack.publish("Send", sends[0])
        assert_that(self.tags(outbox_id), equal_to(["sent"]))
        stored = self.stack.read(f"msgs/{outbox_id}/raw.eml").decode()
        assigned = f"<{sends[0].message_id}@us-east-2.amazonses.com>"
        assert_that(stored, contains_string(f"Message-ID: {assigned}"))
        self.stack.publish("Delivery", sends[0])
        assert_that(self.outbox(outbox_id)["status"], equal_to("delivered"))
        assert_that(self.meta(outbox_id)["status"], equal_to("delivered"))

    def test_reply_threads_and_forwards_attachment(self) -> None:
        """A reply carries In-Reply-To/References and copies an attachment."""
        original = self.deliver(
            message(
                "Contract",
                attachments={"contract.pdf": b"%PDF"},
                message_id="<orig@example.com>",
            )
        )
        read = self.stack.tool("get_message", id=original)
        assert isinstance(read, dict)
        name = read["headers"]["attachments"][0]["name"]
        outbox_id = self.send(
            to=["alice@example.com"],
            subject="Re: Contract",
            body="Signed.",
            in_reply_to_id=original,
            attach_from=[{"id": original, "name": name}],
        )
        raw = self.stack.emulator.sends[0].raw
        sent = email.message_from_bytes(raw, policy=email.policy.default)
        assert_that(str(sent["In-Reply-To"]), equal_to("<orig@example.com>"))
        assert_that(str(sent["References"]), contains_string("<orig@example.com>"))
        attached = [part.get_filename() for part in sent.iter_attachments()]
        assert_that(attached, equal_to(["contract.pdf"]))
        assert_that(self.outbox(outbox_id)["status"], equal_to("sending"))

    def test_bcc_is_enveloped_not_shown(self) -> None:
        """Bcc recipients get the message but are not in its headers."""
        self.send(
            to=["a@example.net"], bcc=["secret@example.net"], subject="x", body="y"
        )
        send = self.stack.emulator.sends[0]
        assert_that(
            sorted(send.recipients), equal_to(["a@example.net", "secret@example.net"])
        )
        assert_that(send.raw.decode(), is_not(contains_string("secret@example.net")))

    def test_bounce(self) -> None:
        """A bounce event marks the message bounced with the reason."""
        outbox_id = self.send(to=["nobody@example.net"], subject="x", body="y")
        send = self.stack.emulator.sends[0]
        bounce = {
            "bounceType": "Permanent",
            "bounceSubType": "General",
            "bouncedRecipients": [{"emailAddress": "nobody@example.net"}],
        }
        self.stack.publish("Bounce", send, bounce=bounce)
        state = self.outbox(outbox_id)
        assert_that(state["status"], equal_to("bounced"))
        assert_that(
            state["error"], contains_string("Permanent/General: nobody@example.net")
        )
        assert_that(self.tags(outbox_id), equal_to(["bounced", "sent"]))

    def test_ses_rejects(self) -> None:
        """SES refusing the message (unverified sender) is terminal: rejected."""
        self.stack.client("sesv2", direct=True).delete_email_identity(
            EmailIdentity=DOMAIN
        )
        outbox_id = self.send(to=["bob@example.net"], subject="x", body="y")
        state = self.outbox(outbox_id)
        assert_that(state["status"], equal_to("rejected"))
        assert_that(state["error"], contains_string("MessageRejected"))
        assert_that(self.tags(outbox_id), equal_to(["rejected"]))

    def test_ses_outage_recovers(self) -> None:
        """SES failing all three attempts: the reconciler replays the event and it sends."""
        self.stack.emulator.fault("POST", r"^/v2/email/outbound-emails", times=3)
        outbox_id = self.send(to=["bob@example.net"], subject="x", body="y")
        assert_that(self.stack.emulator.sends, empty())
        assert_that(self.outbox(outbox_id)["status"], equal_to("queued"))
        assert_that(self.stack.keys("aws/lambda/async/"), has_length(1))
        self.stack.reconcile()
        assert_that(self.stack.emulator.sends, has_length(1))
        assert_that(self.outbox(outbox_id)["status"], equal_to("sending"))
        assert_that(self.stack.keys("aws/"), empty())
        assert_that(self.ops(), empty())

    def test_ses_blip_retried_by_lambda(self) -> None:
        """SES failing once: Lambda's retry sends it straight away."""
        self.stack.emulator.fault("POST", r"^/v2/email/outbound-emails", times=1)
        outbox_id = self.send(to=["bob@example.net"], subject="x", body="y")
        assert_that(self.stack.emulator.sends, has_length(1))
        assert_that(self.outbox(outbox_id)["attempts"], equal_to(2))
        assert_that(self.stack.keys("aws/"), empty())

    def test_stuck_message_is_reported(self) -> None:
        """Persistent SES failure: one ops note (a day) says the message is stuck."""
        self.stack.emulator.fault("POST", r"^/v2/email/outbound-emails", times=10_000)
        outbox_id = self.send(to=["bob@example.net"], subject="x", body="y")
        for _ in range(4):
            self.stack.advance(31 * 60)
            self.stack.reconcile()
        subjects = [str(note["subject"]) for note in self.ops()]
        stuck = [subject for subject in subjects if f"{outbox_id} is stuck" in subject]
        assert_that(stuck, has_length(1))
        assert_that(self.outbox(outbox_id)["status"], is_not(equal_to("sent")))

    def test_lost_send_event(self) -> None:
        """SES accepted it but the Send event never came: the reconciler marks it sent."""
        outbox_id = self.send(to=["bob@example.net"], subject="x", body="y")
        self.stack.advance(31 * 60)
        self.stack.reconcile()
        assert_that(self.outbox(outbox_id)["status"], equal_to("sent"))
        assert_that(self.stack.emulator.sends, has_length(1))  # not sent twice

    def test_invalid_arguments(self) -> None:
        """Tool errors come back as MCP tool errors, and nothing is queued."""
        with self.assertRaisesRegex(ToolFailed, "at least one"):
            self.send(to=[], subject="x", body="y")
        assert_that(self.stack.keys("outbox/"), empty())


class TestTagsAndIndex(EndToEnd):
    """Tagging, the indexer and its failure reporting."""

    def test_tagging(self) -> None:
        """Archive by removing inbox, file under a folder tag; bad tags are refused."""
        ulid = self.deliver(message("Tag me"))
        result = self.stack.tool(
            "tag_email", id=ulid, add=["receipts"], remove=["inbox"]
        )
        assert_that(result, equal_to({"id": ulid, "tags": ["receipts"]}))
        assert_that(self.search(tags=["inbox"]), empty())
        assert_that([h["id"] for h in self.search(tags=["receipts"])], equal_to([ulid]))
        with self.assertRaises(ToolFailed):
            self.stack.tool("tag_email", id=ulid, add=["Not A Tag!"])
        with self.assertRaises(ToolFailed):
            self.stack.tool("tag_email", id="01ARZ3NDEKTSV4RRFFQ69G5FAV", add=["x"])

    def test_index_and_ranked_search(self) -> None:
        """After indexing, search is ranked and new mail still shows up from the tail."""
        first = self.deliver(message("Invoice for October", "Please pay the invoice."))
        self.deliver(message("Lunch?", "Are you free?"))
        assert_that(self.stack.index().ok, equal_to(True))
        assert_that(self.stack.keys("unindexed/"), empty())
        assert_that(self.stack.keys("index/"), is_not(empty()))
        hits = self.search(query="invoice")
        assert_that([(h["id"], h["ranked"]) for h in hits], equal_to([(first, True)]))
        later = self.deliver(message("Second invoice", "Another invoice."))
        ids = {h["id"]: h["ranked"] for h in self.search(query="invoice")}
        assert_that(ids, equal_to({first: True, later: False}))
        tags = self.stack.tool("list_tags")
        assert_that(tags, equal_to({"tags": {"inbox": 3}}))

    def test_threshold_starts_indexer(self) -> None:
        """The hundredth unindexed message invokes the indexer asynchronously."""
        for number in range(100):
            self.stack.receive(message(f"Bulk {number}"))
        results = self.stack.settle()
        assert_that([r.error for r in results if not r.ok], empty())
        assert_that(self.stack.keys("unindexed/"), empty())

    def test_indexer_failures_are_reported(self) -> None:
        """A failing indexer run (Lambda retries it) produces one ops note a day."""
        self.deliver(message("Something"))
        self.stack.emulator.fault("PUT", r"/index/", times=1000)
        assert_that(self.stack.index().ok, equal_to(False))
        self.stack.index()
        notes = self.ops()
        assert_that(notes, has_length(1))
        assert_that(notes[0]["subject"], contains_string("index"))


class TestReplay(EndToEnd):
    """Events Lambda gave up on are replayed, then parked and reported."""

    def test_replay_then_park(self) -> None:
        """An event that keeps failing is parked after three replays, with one ops note."""
        bad = {"action": "no-such-action"}
        assert_that(self.stack.invoke_async(bad).ok, equal_to(False))
        assert_that(self.stack.keys("aws/lambda/async/"), has_length(1))
        for _ in range(3):
            self.stack.reconcile()
        assert_that(self.stack.keys("aws/"), empty())
        assert_that(self.stack.keys("failed-events/"), has_length(1))
        notes = self.ops()
        assert_that(notes, has_length(1))
        assert_that(
            notes[0]["subject"], contains_string("failed processing repeatedly")
        )

    def test_ops_mail_is_addressed_to_owner(self) -> None:
        """Ops notes land in the mailbox, from Pidge, tagged inbox and ops."""
        self.stack.invoke_async({"action": "no-such-action"})
        for _ in range(3):
            self.stack.reconcile()
        note = self.ops()[0]
        assert_that(sorted(note["tags"]), equal_to(["inbox", "ops"]))  # type: ignore[arg-type]
        read = self.stack.tool("get_message", id=note["id"])
        assert isinstance(read, dict)
        assert_that(read["body"], contains_string("failed-events/"))


class TestKillSwitch(EndToEnd):
    """The budget kill switch stops receiving and stops the function."""

    def test_trip(self) -> None:
        """Receiving is deactivated and concurrency set to 0; then nothing runs."""
        ses = self.stack.client("ses", direct=True)
        assert_that(
            ses.describe_active_receipt_rule_set().get("Metadata"), is_not(None)
        )
        result = self.stack.notify(KILL_SWITCH_TOPIC, "Budget exceeded")
        assert_that(result.ok, equal_to(True))
        assert_that(
            result.payload,
            equal_to({"receipt_rule_set": "deactivated", "reserved_concurrency": 0}),
        )
        assert_that(
            ses.describe_active_receipt_rule_set().get("Metadata"), equal_to(None)
        )
        assert_that(self.stack.emulator.concurrency, equal_to({"pidge": 0}))
        logs = self.stack.logs()
        assert_that(logs, contains_string("SES receipt rule set deactivated"))
        assert_that(logs, contains_string("reserved concurrency set to 0"))
        with self.assertRaisesRegex(AssertionError, "TooManyRequests"):
            self.stack.rpc("tools/list")

    def test_idempotent(self) -> None:
        """Tripping twice (SNS can deliver twice) leaves the same end state."""
        for _ in range(2):
            self.stack.emulator.concurrency.clear()  # let the second delivery run
            assert_that(
                self.stack.notify(KILL_SWITCH_TOPIC, "again").ok, equal_to(True)
            )
        ses = self.stack.client("ses", direct=True)
        assert_that(
            ses.describe_active_receipt_rule_set().get("Metadata"), equal_to(None)
        )
        assert_that(self.stack.emulator.concurrency, equal_to({"pidge": 0}))

    def test_other_topics_are_ignored(self) -> None:
        """An SNS record from another topic does not trip the switch."""
        other = KILL_SWITCH_TOPIC.replace("budget-killswitch", "something-else")
        assert_that(self.stack.notify(other, "hello").ok, equal_to(False))
        ses = self.stack.client("ses", direct=True)
        assert_that(
            ses.describe_active_receipt_rule_set().get("Metadata"), is_not(None)
        )
        assert_that(self.stack.emulator.concurrency, equal_to({}))


if __name__ == "__main__":
    unittest.main()
