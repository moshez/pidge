"""The batch indexer: fold ``unindexed/`` and ``dirty/`` messages into shards."""

import collections
import contextlib
import pathlib
import tempfile
import time
from collections.abc import Iterable, Sequence

from . import jsonvalue, ulid
from .ops import Problem, report_problem
from .ports import Context
from .docstore import INDEX_PREFIX, LIST_LIMIT, fetch_batch, marker_id, pending_markers
from .documents import IndexDoc
from .shard import open_shard, upsert
from .store import Listed, NotFound, PreconditionFailed

PUT_ATTEMPTS = 5
ORPHAN_AGE = 3600
FAILURES_KEY = "ops-state/index-failures.json"


def shard_key(year: int) -> str:
    """Return the key of a year's shard.

    Args:
        year: The year.

    Returns:
        The S3 key.
    """
    return f"{INDEX_PREFIX}index-{year}.db"


def year_of(message_id: str) -> int:
    """Return the shard year of a message, from its ULID.

    Args:
        message_id: The ULID.

    Returns:
        The UTC year it was created in.
    """
    return time.gmtime(ulid.timestamp(message_id)).tm_year


def rebuild(workdir: str, shard: bytes, docs: Iterable[IndexDoc]) -> bytes:
    """Apply documents to a shard image.

    Args:
        workdir: A scratch directory.
        shard: The current shard bytes (empty for a new shard).
        docs: The documents to upsert.

    Returns:
        The new shard bytes.
    """
    with tempfile.TemporaryDirectory(dir=workdir) as scratch:
        path = pathlib.Path(scratch) / "shard.db"
        path.write_bytes(shard)
        apply_docs(str(path), docs)
        return path.read_bytes()


def apply_docs(path: str, docs: Iterable[IndexDoc]) -> None:
    """Upsert documents into a shard file and compact it.

    Args:
        path: The shard file.
        docs: The documents.
    """
    with contextlib.closing(open_shard(path)) as connection:
        for item in docs:
            upsert(connection, item)
        connection.commit()
        connection.execute("VACUUM")


def try_write(context: Context, key: str, docs: Sequence[IndexDoc]) -> bool:
    """Make one compare-and-swap attempt at updating a shard.

    Args:
        context: The flow context.
        key: The shard key.
        docs: The documents.

    Returns:
        True if the write succeeded, False if it lost a race.
    """
    store = context.store
    try:
        current = store.get(key)
    except NotFound:
        with contextlib.suppress(PreconditionFailed):
            store.put_new(key, rebuild(context.workdir, b"", docs))
            return True
        return False
    image = rebuild(context.workdir, current.body, docs)
    with contextlib.suppress(PreconditionFailed):
        store.replace(key, image, current.etag)
        return True
    return False


def write_shard(context: Context, year: int, docs: Sequence[IndexDoc]) -> None:
    """Upsert documents into a year's shard, retrying lost races.

    Args:
        context: The flow context.
        year: The shard year.
        docs: The documents.

    Raises:
        PreconditionFailed: If every attempt lost a race.
    """
    key = shard_key(year)
    for _ in range(PUT_ATTEMPTS):
        if try_write(context, key, docs):
            return
    raise PreconditionFailed(key)


def clear_markers(context: Context, markers: Iterable[Listed]) -> None:
    """Delete processed markers, unless they were rewritten meanwhile.

    Args:
        context: The flow context.
        markers: The markers that were processed.
    """
    for listed in markers:
        with contextlib.suppress(PreconditionFailed, NotFound):
            context.store.delete_if(listed.key, listed.etag)


def run_indexer(context: Context) -> int:
    """Index every message that has a marker.

    Args:
        context: The flow context.

    Returns:
        The number of documents indexed.
    """
    markers = pending_markers(context.store, LIST_LIMIT)
    docs = fetch_batch(context.store, (marker_id(listed) for listed in markers))
    by_year: dict[int, list[Listed]] = collections.defaultdict(list)
    cutoff = context.clock() - ORPHAN_AGE
    for listed in markers:
        if docs[marker_id(listed)] is not None or listed.modified < cutoff:
            by_year[year_of(marker_id(listed))].append(listed)
    count = 0
    for year, group in sorted(by_year.items()):
        ready = {marker_id(listed): docs[marker_id(listed)] for listed in group}
        present = [doc for doc in ready.values() if doc is not None]
        write_shard(context, year, present)
        clear_markers(context, group)
        count += len(present)
    return count


def record_failure(context: Context, error: Exception) -> None:
    """Count a failed indexer run; report repeated failures as ops mail.

    Args:
        context: The flow context.
        error: What went wrong.
    """
    previous = jsonvalue.loads_dict(
        context.store.get(FAILURES_KEY).body
        if context.store.list(FAILURES_KEY, 1)
        else b"{}"
    )
    failures = jsonvalue.next_count(previous, "failures")
    context.store.put(
        FAILURES_KEY, jsonvalue.dumps(dict(failures=failures, error=repr(error)))
    )
    if failures >= 2:
        problem = Problem(
            key="index-failing",
            subject=f"The search indexer has failed {failures} times in a row",
            details=f"Last error: {error!r}",
        )
        report_problem(context, problem)


def run_tracked(context: Context) -> int:
    """Run the indexer, writing an ops message after repeated failures.

    Args:
        context: The flow context.

    Returns:
        The number of documents indexed.

    Raises:
        Exception: Whatever the indexer raised.
    """
    try:
        count = run_indexer(context)
    except Exception as error:
        record_failure(context, error)
        raise
    context.store.delete(FAILURES_KEY)
    return count
