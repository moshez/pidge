"""Deterministic HTML renderers for pidge mail: an inbox list and one message.

Renderers are pure: no network, no clock. Fragment output (the default) uses
only CSS variables and loads nothing external, so it can be shown in Claude's
inline widget; :func:`as_page` wraps a fragment in a standalone document.

The command line renders saved tool output (a ``get_message`` object, a list
of them, search hits, or a ``{"results": [...]}`` search response)::

    python -m pidge_client.views inbox MSG.json... [--page] [--tz ZONE] [--now EPOCH]
    python -m pidge_client.views message MSG.json [--html BODY.html] [--page]
"""

import argparse
import datetime
import html
import re
import sys
import time
import zoneinfo
from collections.abc import Callable, Iterable, Sequence
from typing import TextIO

from .dates import full_date, when
from .mail import Mail, as_mail, load_files, read_file
from .models import Hit, Message
from .reader import drop_pixels, readable_html
from .text import bare_address, display_name, initials, snippet

PAGE_CSS = """
:root{--text-primary:#1a1a1a;--text-secondary:#666;--text-muted:#888;
--text-accent:#185FA5;--border:rgba(0,0,0,.1);--bg-neutral:#f1efe8;
--surface-2:#fff;--radius:8px;--font-sans:system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root{--text-primary:#eee;--text-secondary:#999;
--text-muted:#777;--text-accent:#85B7EB;--border:rgba(255,255,255,.1);
--bg-neutral:#2c2c2a;--surface-2:#1f1f1f}}
body{font-family:var(--font-sans);color:var(--text-primary);max-width:760px;
margin:2rem auto;padding:0 1rem;background:var(--surface-2)}
"""
DEFAULT_TZ = "America/Los_Angeles"
OUTGOING = "out"
URL = re.compile(r"(https?://[^\s<]+)")
CLIP = ' <i class="ti ti-paperclip" aria-hidden="true"></i>'
ROW_STYLE = (
    "display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 12px;"
    "padding:10px 4px;border-bottom:0.5px solid var(--border)"
)
ELLIPSIS = "white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
AVATAR_STYLE = (
    "flex:none;width:40px;height:40px;border-radius:50%;"
    "background:var(--bg-neutral);display:flex;align-items:center;"
    "justify-content:center;font-size:14px;font-weight:500;"
    "color:var(--text-secondary)"
)
CARD_STYLE = (
    "background:var(--surface-2);border:0.5px solid var(--border);"
    "border-radius:12px;padding:1rem 1.25rem;font-size:14px;line-height:1.6"
)
FRAME_STYLE = (
    "width:100%;height:80vh;border:0.5px solid var(--border);"
    "border-radius:12px;background:#fff"
)


def chip(label: str) -> str:
    """Render a tag chip.

    Args:
        label: The tag.

    Returns:
        The HTML.
    """
    return (
        '<span style="font-size:11px;padding:1px 6px;border-radius:var(--radius);'
        'background:var(--bg-neutral);color:var(--text-secondary)">'
        f"{html.escape(label)}</span>"
    )


def outgoing(mail: Mail) -> bool:
    """Tell whether a message was sent (rather than received).

    Args:
        mail: The message.

    Returns:
        True for sent mail.
    """
    return mail.direction == OUTGOING


def row_who(mail: Mail) -> str:
    """Name the other party: the sender, or the recipients of sent mail.

    Args:
        mail: The message.

    Returns:
        The escaped text.
    """
    if outgoing(mail):
        return html.escape("To: " + ", ".join(map(display_name, mail.to)))
    return html.escape(display_name(mail.sender))


def row_footer(mail: Mail) -> str:
    """Render a row's recipients (received mail only) and tags other than inbox.

    Args:
        mail: The message.

    Returns:
        The HTML.
    """
    to_line = ", ".join(map(bare_address, mail.to))
    shown = not outgoing(mail) and to_line
    recipients = "to " + html.escape(to_line) if shown else ""
    return recipients + "".join(chip(tag) for tag in mail.tags if tag != "inbox")


def inbox_row(mail: Mail, now: datetime.datetime, tz: datetime.tzinfo) -> str:
    """Render one list row.

    Args:
        mail: The message.
        now: The current time.
        tz: The time zone to show.

    Returns:
        The HTML.
    """
    weight = 400 if outgoing(mail) else 500
    clip = CLIP if mail.attachments else ""
    title = html.escape(full_date(mail.date, tz))
    subject = html.escape(mail.subject)
    return (
        f'<div style="{ROW_STYLE}">'
        f'<div style="font-size:14px;font-weight:{weight};{ELLIPSIS}">'
        f"{row_who(mail)}{clip}</div>"
        '<div style="font-size:12px;color:var(--text-muted);white-space:nowrap" '
        f'title="{title}">{when(mail.date, now, tz)}</div>'
        f'<div style="grid-column:1/-1;font-size:14px;{ELLIPSIS}">{subject} '
        '<span style="color:var(--text-secondary)">'
        f"— {html.escape(snippet(mail.text))}</span></div>"
        '<div style="grid-column:1/-1;display:flex;gap:6px;align-items:center;'
        f'font-size:11px;color:var(--text-muted)">{row_footer(mail)}</div>'
        "</div>"
    )


def render_inbox(
    messages: Iterable[Message | Hit],
    now: datetime.datetime,
    tz: datetime.tzinfo | None = None,
) -> str:
    """Render a newest-first list of messages, one bordered row each.

    Args:
        messages: Messages or search hits.
        now: The current time (for "today" and "this week"); naive means UTC.
        tz: The time zone to show; defaults to ``now``'s.

    Returns:
        An HTML fragment.
    """
    aware = now if now.tzinfo else now.replace(tzinfo=datetime.UTC)
    zone = tz or aware.tzinfo or datetime.UTC
    mails = sorted(map(as_mail, messages), key=lambda m: (m.date, m.id), reverse=True)
    rows = "".join(inbox_row(mail, aware, zone) for mail in mails)
    summary = f"{len(mails)} message{'' if len(mails) == 1 else 's'}"
    return (
        f'<h2 class="sr-only">Mailbox list, {summary}, newest first</h2>'
        '<div style="font-size:12px;color:var(--text-muted);padding:0 4px 6px">'
        f"{summary}</div>{rows}"
    )


def text_to_html(text: str) -> str:
    """Render a plain-text body: escape, link URLs, keep paragraph breaks.

    Args:
        text: The body text.

    Returns:
        The HTML.
    """
    escaped = html.escape(text.replace("\r\n", "\n").strip())
    linked = URL.sub(r'<a href="\1">\1</a>', escaped)
    return "".join(
        f'<p style="margin:0 0 10px">{para.replace(chr(10), "<br>")}</p>'
        for para in re.split(r"\n{2,}", linked)
        if para.strip()
    )


def header_rows(mail: Mail, tz: datetime.tzinfo) -> Sequence[tuple[str, str]]:
    """List a message's header table: From, To, Date, then Status and Attachments.

    Args:
        mail: The message.
        tz: The time zone to show.

    Returns:
        Labels and values.
    """
    optional = [("Status", mail.status), ("Attachments", ", ".join(mail.attachments))]
    return [
        ("From", mail.sender),
        ("To", ", ".join(mail.to)),
        ("Date", full_date(mail.date, tz)),
        *[(key, value) for key, value in optional if value],
    ]


def header_table(mail: Mail, tz: datetime.tzinfo) -> str:
    """Render a message's header table.

    Args:
        mail: The message.
        tz: The time zone to show.

    Returns:
        The HTML.
    """
    rows = "".join(
        '<tr><td style="color:var(--text-secondary);padding:2px 12px 2px 0;'
        f'vertical-align:top">{key}</td>'
        '<td style="padding:2px 0;word-break:break-word">'
        f"{html.escape(value)}</td></tr>"
        for key, value in header_rows(mail, tz)
    )
    return f'<table style="font-size:13px;border-collapse:collapse">{rows}</table>'


def message_header(mail: Mail, tz: datetime.tzinfo) -> str:
    """Render a message's subject, avatar, header table and tags.

    Args:
        mail: The message.
        tz: The time zone to show.

    Returns:
        The HTML.
    """
    avatar = html.escape(initials(display_name(mail.sender)))
    chips = "".join(map(chip, mail.tags))
    return (
        '<p style="font-size:18px;font-weight:500;margin:0 0 12px">'
        f"{html.escape(mail.subject)}</p>"
        '<div style="display:flex;gap:12px;align-items:flex-start;margin-bottom:12px">'
        f'<div style="{AVATAR_STYLE}">{avatar}</div>{header_table(mail, tz)}</div>'
        f'<div style="display:flex;gap:6px;margin-bottom:12px">{chips}</div>'
    )


def framed(body_html: str) -> str:
    """Frame original HTML in a sandboxed iframe, minus tracking pixels.

    Args:
        body_html: The original HTML.

    Returns:
        The iframe.
    """
    source = '<base target="_blank">' + drop_pixels(body_html)
    return (
        '<iframe sandbox="allow-popups allow-popups-to-escape-sandbox" '
        f'referrerpolicy="no-referrer" style="{FRAME_STYLE}" '
        f'srcdoc="{html.escape(source, quote=True)}"></iframe>'
    )


def readable_body(mail: Mail, body_html: str | None) -> str:
    """Render a message's body for chat: the readable HTML, else the text.

    Args:
        mail: The message.
        body_html: The original HTML body, or None.

    Returns:
        The HTML.
    """
    return readable_html(body_html) if body_html else text_to_html(mail.text)


def render_message(
    message: Message | Hit,
    body_html: str | None,
    tz: datetime.tzinfo,
    page: bool = False,  # noqa: SLD609
) -> str:
    """Render one message.

    Args:
        message: The message (or a search hit, showing its snippet).
        body_html: The original HTML body (``0-body.html``), or None.
        tz: The time zone to show.
        page: Frame the original HTML in a sandboxed iframe (for a standalone
            page) instead of reducing it to readable paragraphs (for chat).

    Returns:
        An HTML fragment.
    """
    mail = as_mail(message)
    if page and body_html:
        body = framed(body_html)
    else:
        body = readable_body(mail, body_html)
    return (
        f'<div style="{CARD_STYLE}">'
        f'<h2 class="sr-only">Email: {html.escape(mail.subject)}</h2>'
        f"{message_header(mail, tz)}"
        '<div style="border-top:0.5px solid var(--border);padding-top:12px;'
        f'overflow-wrap:anywhere">{body}</div></div>'
    )


def as_page(title: str, fragment: str) -> str:
    """Wrap a fragment in a standalone HTML document that defines its variables.

    Args:
        title: The page title.
        fragment: The fragment.

    Returns:
        The document.
    """
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        f"<style>{PAGE_CSS}.sr-only{{position:absolute;left:-9999px}}"
        "a{color:var(--text-accent)}</style>"
        f"</head><body>{fragment}</body></html>"
    )


def parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The parser.
    """
    top = argparse.ArgumentParser(prog="python -m pidge_client.views")
    top.add_argument("--tz", default=DEFAULT_TZ)
    top.add_argument("--page", action="store_true", help="standalone HTML document")
    top.add_argument("--now", type=int, help="epoch seconds for 'now'")
    commands = top.add_subparsers(dest="command", required=True)
    inbox = commands.add_parser("inbox")
    inbox.add_argument("files", nargs="+")
    message = commands.add_parser("message")
    message.add_argument("file")
    message.add_argument("--html", help="the message's 0-body.html")
    return top


def render_command(options: argparse.Namespace, now: datetime.datetime) -> str:
    """Render what the command line asked for.

    Args:
        options: The parsed arguments.
        now: The current time, in the chosen zone.

    Returns:
        The fragment, or the page with ``--page``.
    """
    zone = now.tzinfo or datetime.UTC
    if options.command == "inbox":
        rendered, title = render_inbox(load_files(options.files), now, zone), "Mail"
    else:
        (item,) = load_files([options.file])
        body_html = read_file(options.html) if options.html else None
        rendered = render_message(item, body_html, zone, options.page)
        title = as_mail(item).subject
    return as_page(title, rendered) if options.page else rendered


def run(argv: Sequence[str], out: TextIO, clock: Callable[[], float]) -> int:
    """Run the command line.

    Args:
        argv: The arguments, without the program name.
        out: Where to write the HTML.
        clock: Supplies the current time when ``--now`` is not given.

    Returns:
        The exit status.
    """
    options = parser().parse_args(argv)
    stamp = clock() if options.now is None else options.now
    now = datetime.datetime.fromtimestamp(stamp, zoneinfo.ZoneInfo(options.tz))
    out.write(render_command(options, now))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run(sys.argv[1:], sys.stdout, time.time))
