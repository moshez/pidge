"""Reduce email HTML to a small, styleless, readable subset."""

import dataclasses
import html
import html.parser
import re
from collections.abc import Callable, Mapping, MutableSequence, Sequence

SKIP = frozenset({"head", "style", "script", "title", "noscript"})
HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
BLOCK = frozenset(
    {"p", "div", "td", "th", "tr", "table", "section", "center", "blockquote", "li"}
    | HEADINGS
)
HIDEABLE = frozenset({"div", "span", "td", "table"})
INLINE = {"b": "strong", "strong": "strong", "i": "em", "em": "em"}
LINK_SCHEMES = ("http://", "https://", "mailto:")
HEADING_MARK = "\x00h"
START_TEXT = {
    "br": "\n",
    **dict.fromkeys(BLOCK, "\n"),
    **dict.fromkeys(HEADINGS, "\n" + HEADING_MARK),
    **{tag: "<" + name + ">" for tag, name in INLINE.items()},
}
END_TEXT = dict(
    dict.fromkeys(BLOCK, "\n"), **{tag: "</" + INLINE[tag] + ">" for tag in INLINE}
)
HEADING_STYLE = "font-weight:500;font-size:16px"
PIXEL_SIZE = 2
TINY_LINK_CHARS = 3
IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
ATTRIBUTE = re.compile(r"""([\w:-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")
STYLE_SIZE = re.compile(r"(?:^|;)(?:max-)?(?:width|height):(\d+)(?:px)?(?=;|$)")
TINY_LINK = re.compile(r'<a href="[^"]*">([^<]*)</a>')
MARKUP = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")

Block = tuple[bool, str]


def leading_int(value: str) -> int | None:
    """Read the number at the start of an attribute value.

    Args:
        value: The value, such as ``1``, ``1px`` or ``100%``.

    Returns:
        The number, or None if there is none.
    """
    digits = re.match(r"\s*(\d+)", value)
    return None if digits is None else int(digits.group(1))


def is_pixel(attrs: Mapping[str, str]) -> bool:
    """Tell whether an image is a tracking pixel (2px or less on a side).

    Args:
        attrs: The image's attributes.

    Returns:
        True for a tracking pixel.
    """
    sizes = [leading_int(attrs.get(name, "")) for name in ("width", "height")]
    style = attrs.get("style", "").replace(" ", "").lower()
    sizes.extend(int(size) for size in STYLE_SIZE.findall(style))
    return any(size is not None and size <= PIXEL_SIZE for size in sizes)


def tag_attributes(tag: str) -> Mapping[str, str]:
    """Read the attributes of one start tag.

    Args:
        tag: The tag's source text.

    Returns:
        Attribute values by lower-case name, unquoted and unescaped.
    """
    return {
        name.lower(): html.unescape(value.strip("\"'"))
        for name, value in ATTRIBUTE.findall(tag)
    }


def drop_pixels(source: str) -> str:
    """Remove tracking-pixel images from HTML, leaving the rest as it is.

    Args:
        source: The HTML.

    Returns:
        The HTML without images 2px or less on a side.
    """
    return IMG_TAG.sub(
        lambda found: "" if is_pixel(tag_attributes(found.group())) else found.group(),
        source,
    )


def image_markup(attrs: Mapping[str, str], link: MutableSequence[str]) -> str:
    """Describe an image by its alt text.

    Args:
        attrs: The image's attributes.
        link: The open link (unused).

    Returns:
        A muted ``image: ...`` label, or nothing for pixels and unlabeled images.
    """
    alt = attrs.get("alt", "").strip()
    if not alt or is_pixel(attrs):
        return ""
    return (
        '<span style="font-size:12px;color:var(--text-muted);border:0.5px dashed '
        'var(--border);border-radius:4px;padding:0 4px">'
        f"image: {html.escape(alt)}</span>"
    )


def link_markup(attrs: Mapping[str, str], link: MutableSequence[str]) -> str:
    """Open a link, if its URL is a web or mail address.

    Args:
        attrs: The link's attributes.
        link: The open link's URL (set here).

    Returns:
        The opening tag, or nothing for other URLs.
    """
    link[:] = [
        href for href in [attrs.get("href", "")] if href.startswith(LINK_SCHEMES)
    ]
    return "".join(f'<a href="{html.escape(href)}">' for href in link)


def no_markup(attrs: Mapping[str, str], link: MutableSequence[str]) -> str:
    """Drop a tag that has no readable form.

    Args:
        attrs: The tag's attributes.
        link: The open link (unused).

    Returns:
        Nothing.
    """
    return ""


STARTS: Mapping[str, Callable[[Mapping[str, str], MutableSequence[str]], str]] = {
    "a": link_markup,
    "img": image_markup,
}


def start_markup(tag: str, attrs: Mapping[str, str], link: MutableSequence[str]) -> str:
    """Translate a visible start tag.

    Args:
        tag: The tag name.
        attrs: Its attributes.
        link: The open link's URL, if any.

    Returns:
        The markup to emit.
    """
    fixed = START_TEXT.get(tag)
    if fixed is not None:
        return fixed
    return STARTS.get(tag, no_markup)(attrs, link)


def end_markup(tag: str, link: MutableSequence[str]) -> str:
    """Translate a visible end tag.

    Args:
        tag: The tag name.
        link: The open link's URL, if any (cleared when it closes).

    Returns:
        The markup to emit.
    """
    if tag == "a" and link:
        link.clear()
        return "</a>"
    return END_TEXT.get(tag, "")


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Readable:
    """Collects the readable parts of email HTML as marked-up lines.

    Keeps text, links, emphasis and headings; turns block containers
    (including layout tables) into line breaks; replaces images with their
    alt text and drops tracking pixels; drops ``<head>``, ``<style>``,
    ``<script>`` and hidden preheaders. Construct it with :meth:`create`.
    """

    _out: list[str]
    _skip: list[str]
    _hidden: list[bool]
    _link: list[str]

    @classmethod
    def create(cls) -> "Readable":
        """Create an empty collector.

        Returns:
            The collector.
        """
        return cls(_out=[], _skip=[], _hidden=[], _link=[])

    def invisible(self) -> bool:
        """Tell whether the parser is inside skipped or hidden content.

        Returns:
            True if content here is not shown.
        """
        return bool(self._skip) or any(self._hidden)

    def start(self, tag: str, attrs: Sequence[tuple[str, str | None]]) -> None:
        """Record a start tag.

        Args:
            tag: The tag name.
            attrs: Its attributes.
        """
        values = {name: value or "" for name, value in attrs}
        if tag in SKIP:
            self._skip.append(tag)
            return
        if tag in HIDEABLE:
            style = values.get("style", "").replace(" ", "").lower()
            self._hidden.append("display:none" in style or "max-height:0" in style)
        if not self.invisible():
            self._out.append(start_markup(tag, values, self._link))

    def end(self, tag: str) -> None:
        """Record an end tag.

        Args:
            tag: The tag name.
        """
        if tag in SKIP:
            del self._skip[-1:]
            return
        if tag in HIDEABLE and self._hidden and self._hidden.pop():
            return
        if not self.invisible():
            self._out.append(end_markup(tag, self._link))

    def data(self, text: str) -> None:
        """Record text.

        Args:
            text: The text.
        """
        if not self.invisible():
            self._out.append(html.escape(SPACE.sub(" ", text)))

    def lines(self) -> Sequence[str]:
        """Return the collected markup, one block per line.

        Returns:
            The lines, some empty.
        """
        return "".join(self._out).split("\n")


def tiny_link(text: str) -> bool:
    """Tell whether a block is one short link, like a survey scale's ``7``.

    Args:
        text: The block's markup.

    Returns:
        True for a link with at most three visible characters.
    """
    found = TINY_LINK.fullmatch(text)
    return found is not None and 0 < len(found.group(1).strip()) <= TINY_LINK_CHARS


def flush(run: Sequence[str]) -> Sequence[Block]:
    """Turn a run of tiny links into blocks: one line if there are several.

    Args:
        run: The tiny links' markup.

    Returns:
        The blocks.
    """
    if len(run) > 1:
        return [(False, " ".join(run))]
    return [(False, single) for single in run]


def collapse_tiny_links(blocks: Sequence[Block]) -> Sequence[Block]:
    """Join runs of tiny link-only blocks (such as 0-10 scales) into one line.

    Args:
        blocks: Heading flags and markup, one per block.

    Returns:
        The blocks, with each run of two or more tiny links merged.
    """
    merged: list[Block] = []
    run: list[str] = []
    for heading, text in blocks:
        if not heading and tiny_link(text):
            run.append(text)
            continue
        merged.extend(flush(run))
        run = []
        merged.append((heading, text))
    merged.extend(flush(run))
    return merged


def visible_blocks(lines: Sequence[str]) -> Sequence[Block]:
    """Keep the lines with visible text, dropping repeats of the previous one.

    Args:
        lines: The collected lines.

    Returns:
        Heading flags and markup, one per block.
    """
    found: list[Block] = []
    previous = None
    for raw in lines:
        heading = raw.startswith(HEADING_MARK)
        text = raw.replace(HEADING_MARK, "").strip()
        visible = MARKUP.sub("", text).strip(" |·•")
        if visible and text != previous:
            found.append((heading, text))
            previous = text
    return found


def collect(source: str) -> Sequence[str]:
    """Parse HTML and collect its readable markup.

    Args:
        source: The HTML.

    Returns:
        The markup, one block per line.
    """
    readable = Readable.create()
    parser = html.parser.HTMLParser(convert_charrefs=True)
    setattr(parser, "handle_starttag", readable.start)
    setattr(parser, "handle_endtag", readable.end)
    setattr(parser, "handle_data", readable.data)
    parser.feed(source)
    parser.close()
    return readable.lines()


def readable_html(source: str) -> str:
    """Reduce email HTML to readable paragraphs.

    Args:
        source: The original HTML.

    Returns:
        A sequence of ``<p>`` elements styled only with CSS variables.
    """
    blocks = collapse_tiny_links(visible_blocks(collect(source)))
    return "".join(
        f'<p style="margin:0 0 10px;{HEADING_STYLE if heading else ""}">{text}</p>'
        for heading, text in blocks
    )
