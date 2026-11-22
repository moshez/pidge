"""Tags: folders and archive."""

import collections
import contextlib
import sqlite3
from collections.abc import Mapping, Sequence

from .records import retag, update_meta
from .docstore import shard_keys, tail_docs
from .ports import ShardSource
from .shard import tag_counts
from .store import ObjectStore


class TagError(Exception):
    """A tag change was invalid."""


def clean_tags(tags: Sequence[str]) -> Sequence[str]:
    """Validate and normalize tag names.

    Args:
        tags: The requested tags.

    Returns:
        The tags, stripped.

    Raises:
        TagError: If a tag is empty or contains whitespace.
    """
    cleaned = [name.strip() for name in tags]
    for name in cleaned:
        if not name or len(name.split()) != 1:
            raise TagError(f"invalid tag: {name!r}")
    return cleaned


def tag_email(
    store: ObjectStore, message_id: str, add: Sequence[str], remove: Sequence[str]
) -> Sequence[str]:
    """Add and remove tags on a message.

    Args:
        store: The mail bucket.
        message_id: The ULID.
        add: Tags to add.
        remove: Tags to remove.

    Returns:
        The message's new tags.
    """
    change = retag(clean_tags(add), clean_tags(remove))
    return update_meta(store, message_id, change).tags


def list_tags(store: ObjectStore, cache: ShardSource) -> Mapping[str, int]:
    """Count messages per tag.

    Args:
        store: The mail bucket.
        cache: The local shard cache.

    Returns:
        Message counts by tag.
    """
    tail = tail_docs(store)
    counts: collections.Counter[str] = collections.Counter()
    for key in shard_keys(store):
        path = cache.fetch(store, key)
        if not path:
            continue
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        with contextlib.closing(connection):
            counts.update(tag_counts(connection, set(tail)))
    for doc in tail.values():
        counts.update(doc.meta.tags)
    return dict(sorted(counts.items()))
