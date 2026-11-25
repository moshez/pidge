"""How search results are ordered, and where a page of them ends."""

import dataclasses
import enum
from collections.abc import Sequence


class Order(enum.Enum):
    """How search results are listed.

    Attributes:
        DATE: Newest first by ``(date, ulid)``; query terms only filter.
        RELEVANCE: By BM25 score when the query has terms.
    """

    DATE = enum.auto()
    RELEVANCE = enum.auto()


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Position:
    """A place in date order: the last hit a previous page returned.

    Attributes:
        date: The hit's date in epoch seconds.
        ulid: The hit's ID, which breaks ties between equal dates.
    """

    date: int
    ulid: str


def by_date(order: Order, terms: Sequence[str]) -> bool:
    """Check whether results are listed newest first.

    Args:
        order: The requested order.
        terms: The query terms.

    Returns:
        True for date order, and for relevance order without terms.
    """
    return order is Order.DATE or not terms
