"""Convert HTML to readable plain text."""

import dataclasses
import html
import html.parser

_SKIPPED_TAGS = frozenset({"script", "style", "head"})
_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "table"})


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class TextExtractor:
    """Collects the visible text of an HTML document.

    Construct it with :meth:`create` and feed it to :func:`html_to_text`.
    """

    _chunks: list[str]
    _skipping: list[str]

    @classmethod
    def create(cls) -> "TextExtractor":
        """Create an empty extractor.

        Returns:
            The extractor.
        """
        return cls(_chunks=[], _skipping=[])

    def start(self, tag: str) -> None:
        """Record an opening tag.

        Args:
            tag: The tag name.
        """
        if tag in _SKIPPED_TAGS:
            self._skipping.append(tag)
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def end(self, tag: str) -> None:
        """Record a closing tag.

        Args:
            tag: The tag name.
        """
        if self._skipping and self._skipping[-1] == tag:
            self._skipping.pop()

    def data(self, text: str) -> None:
        """Record character data.

        Args:
            text: The text.
        """
        if not self._skipping:
            self._chunks.append(text)

    def text(self) -> str:
        """Return the collected text with whitespace tidied.

        Returns:
            The text.
        """
        joined = "".join(self._chunks)
        lines = (" ".join(line.split()) for line in joined.splitlines())
        return "\n".join(line for line in lines if line)


def html_to_text(markup: str) -> str:
    """Convert HTML to readable plain text.

    Args:
        markup: The HTML.

    Returns:
        The visible text.
    """
    extractor = TextExtractor.create()
    parser = html.parser.HTMLParser(convert_charrefs=True)
    setattr(parser, "handle_starttag", lambda tag, attrs: extractor.start(tag))
    setattr(parser, "handle_endtag", extractor.end)
    setattr(parser, "handle_data", extractor.data)
    parser.feed(markup)
    parser.close()
    return html.unescape(extractor.text())
