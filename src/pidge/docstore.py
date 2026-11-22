"""Reading messages for the index and the tail scan."""

import concurrent.futures
from collections.abc import Iterable, Mapping, Sequence

from . import jsonvalue
from .documents import IndexDoc
from .model import parse_meta
from .ports import Context
from .records import DIRTY, UNINDEXED, read_record
from .store import Listed, NotFound, ObjectStore

THRESHOLD = 100
LIST_LIMIT = 100_000
FETCH_THREADS = 16
INDEX_PREFIX = "index/"


def pending_markers(store: ObjectStore, limit: int) -> Sequence[Listed]:
    """List the index markers.

    Args:
        store: The mail bucket.
        limit: The maximum entries per marker prefix.

    Returns:
        The ``unindexed/`` and ``dirty/`` markers.
    """
    return [*store.list(UNINDEXED, limit), *store.list(DIRTY, limit)]


def marker_id(listed: Listed) -> str:
    """Return the message ID a marker refers to.

    Args:
        listed: The marker.

    Returns:
        The ULID.
    """
    return listed.key.rsplit("/", 1)[-1]


def maybe_trigger_index(context: Context) -> bool:
    """Start the batch indexer if enough messages are waiting.

    Args:
        context: The flow context.

    Returns:
        True if the indexer was invoked.
    """
    waiting = len(pending_markers(context.store, THRESHOLD))
    if waiting < THRESHOLD:
        return False
    context.invoker.invoke(dict(action="index"))
    return True


def fetch_doc(store: ObjectStore, message_id: str) -> IndexDoc | None:
    """Load a message's records for indexing.

    Args:
        store: The mail bucket.
        message_id: The ULID.

    Returns:
        The document, or None if its records are not all there yet.
    """
    try:
        meta = parse_meta(read_record(store, message_id, "meta.json"))
        headers = read_record(store, message_id, "headers.json")
        body = read_record(store, message_id, "body.txt")
    except NotFound:
        return None
    return IndexDoc(
        ulid=message_id,
        headers=jsonvalue.loads_dict(headers),
        body=body.decode("utf-8", errors="replace"),
        meta=meta,
    )


def fetch_batch(
    store: ObjectStore, message_ids: Iterable[str]
) -> Mapping[str, IndexDoc | None]:
    """Load several messages in parallel.

    Args:
        store: The mail bucket.
        message_ids: The ULIDs.

    Returns:
        The documents by ULID; None where records are incomplete.
    """
    unique = sorted(set(message_ids))
    with concurrent.futures.ThreadPoolExecutor(FETCH_THREADS) as pool:
        docs = pool.map(lambda wanted: fetch_doc(store, wanted), unique)
        return dict(zip(unique, docs))


def shard_keys(store: ObjectStore) -> Sequence[str]:
    """List the shards, newest first.

    Args:
        store: The mail bucket.

    Returns:
        The shard keys.
    """
    return sorted(
        (listed.key for listed in store.list(INDEX_PREFIX, LIST_LIMIT)), reverse=True
    )


def tail_docs(store: ObjectStore) -> Mapping[str, IndexDoc]:
    """Load every message that the index does not have current.

    Args:
        store: The mail bucket.

    Returns:
        The documents by ULID.
    """
    ids = [marker_id(listed) for listed in pending_markers(store, LIST_LIMIT)]
    loaded = fetch_batch(store, ids)
    return {key: doc for key, doc in loaded.items() if doc is not None}
