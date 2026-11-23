"""Search: ranked hits from the index shards, plus a scan of the tail."""

import concurrent.futures
import contextlib
import dataclasses
import os
import sqlite3
from collections.abc import Collection, Iterable, Sequence

from .docstore import LIST_LIMIT, marker_id, shard_keys, tail_docs
from .ports import ShardSource
from .records import DIRTY
from .documents import Hit, Query, matches, tail_hit
from .shard import search_shard
from .store import NotFound, NotModified, ObjectStore

SHARD_THREADS = 4


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class ShardCache:
    """Index shards cached in Lambda's ``/tmp`` across warm invocations.

    Construct it with :meth:`create`.
    """

    _directory: str
    _etags: dict[str, str]

    @classmethod
    def create(cls, directory: str) -> "ShardCache":
        """Create a cache rooted at a directory.

        Args:
            directory: Where to keep shard files.

        Returns:
            The cache.
        """
        os.makedirs(directory, exist_ok=True)
        return cls(_directory=directory, _etags={})

    def fetch(self, store: ObjectStore, key: str) -> str:
        """Return a local path to a fresh copy of a shard.

        Args:
            store: The mail bucket.
            key: The shard's key.

        Returns:
            The local file path, or an empty string if the shard is gone.
        """
        path = os.path.join(self._directory, key.replace("/", "_"))
        known = self._etags.get(key, "")
        try:
            found = store.get_if_changed(key, known) if known else store.get(key)
        except NotModified:
            return path
        except NotFound:
            self._etags.pop(key, None)
            return ""
        with open(path, "wb") as stream:
            stream.write(found.body)
        self._etags[key] = found.etag
        return path


def query_shard(path: str, query: Query, exclude: Collection[str]) -> Sequence[Hit]:
    """Run a query against a local shard file.

    Args:
        path: The shard file, or empty for none.
        query: The query.
        exclude: Message IDs to leave out.

    Returns:
        The hits.
    """
    if not path:
        return []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    with contextlib.closing(connection):
        return search_shard(connection, query, exclude)


def newest_first(hits: Iterable[Hit]) -> Sequence[Hit]:
    """Sort hits by date, newest first; ties go to the newer ID.

    Args:
        hits: The hits.

    Returns:
        The sorted hits.
    """
    return sorted(hits, key=lambda hit: (hit.date, hit.ulid), reverse=True)


def merge(ranked: Iterable[Hit], tail: Iterable[Hit], query: Query) -> Sequence[Hit]:
    """Combine index hits and tail hits.

    Args:
        ranked: Hits from the shards.
        tail: Hits from the tail scan.
        query: The query (for ordering and the limit).

    Returns:
        Index hits first (by score, or by date without terms), then tail hits
        by date, without duplicates.
    """
    if query.terms:
        first: Sequence[Hit] = sorted(ranked, key=lambda hit: hit.score or 0.0)
    else:
        first = newest_first(ranked)
    rest = newest_first(tail)
    seen: set[str] = set()
    merged = []
    for hit in [*first, *rest]:
        if hit.ulid not in seen:
            seen.add(hit.ulid)
            merged.append(hit)
    return merged[: query.limit]


def search(store: ObjectStore, cache: ShardSource, query: Query) -> Sequence[Hit]:
    """Search the mailbox.

    Args:
        store: The mail bucket.
        cache: The local shard cache.
        query: The query.

    Returns:
        The hits.
    """
    tail = tail_docs(store)
    exclude = set(tail) | {
        marker_id(listed) for listed in store.list(DIRTY, LIST_LIMIT)
    }
    keys = shard_keys(store)
    paths = [cache.fetch(store, key) for key in keys]
    with concurrent.futures.ThreadPoolExecutor(SHARD_THREADS) as pool:
        results = pool.map(lambda path: query_shard(path, query, exclude), paths)
        ranked = [found for batch in results for found in batch]
    tail_hits = [tail_hit(doc) for doc in tail.values() if matches(doc, query)]
    return merge(ranked, tail_hits, query)
