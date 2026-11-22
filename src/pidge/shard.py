"""SQLite FTS5 index shards: schema, upserts and queries."""

import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence

from . import jsonvalue
from .documents import (
    SNIPPET_CHARS,
    Hit,
    IndexDoc,
    Query,
    addresses_text,
    attachment_names,
    doc_date,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  message_id TEXT,
  in_reply_to TEXT,
  date INTEGER,
  direction TEXT,
  from_addr TEXT,
  to_addrs TEXT,
  cc_addrs TEXT,
  subject TEXT,
  tags TEXT,
  has_attachments INTEGER
);
CREATE TABLE IF NOT EXISTS message_tags (
  id TEXT, tag TEXT, PRIMARY KEY (tag, id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  subject, from_addr, to_addrs, body, attachment_names,
  tokenize='unicode61 remove_diacritics 0'
);
"""


def open_shard(path: str) -> sqlite3.Connection:
    """Open a shard file, creating the schema if needed.

    Args:
        path: The database file.

    Returns:
        The connection.
    """
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    return connection


def delete_doc(connection: sqlite3.Connection, doc_id: str) -> None:
    """Remove a message from a shard.

    Args:
        connection: The shard.
        doc_id: The message ID.
    """
    connection.execute(
        "DELETE FROM fts WHERE rowid IN (SELECT rowid FROM messages WHERE id = ?)",
        (doc_id,),
    )
    connection.execute("DELETE FROM messages WHERE id = ?", (doc_id,))
    connection.execute("DELETE FROM message_tags WHERE id = ?", (doc_id,))


def message_row(doc: IndexDoc) -> Sequence[object]:
    """Build a message's row in the ``messages`` table.

    Args:
        doc: The message.

    Returns:
        The column values.
    """
    headers = doc.headers
    return (
        doc.ulid,
        jsonvalue.get_str(headers, "message_id"),
        jsonvalue.get_str(headers, "in_reply_to"),
        doc_date(doc),
        doc.meta.direction.name.lower(),
        jsonvalue.get_str(headers, "from"),
        addresses_text(doc, "to"),
        addresses_text(doc, "cc"),
        jsonvalue.get_str(headers, "subject"),
        json.dumps(list(doc.meta.tags)),
        int(bool(attachment_names(doc))),
    )


def text_row(doc: IndexDoc) -> Sequence[object]:
    """Build a message's full-text columns.

    Args:
        doc: The message.

    Returns:
        Subject, from, recipients, body and attachment names.
    """
    recipients = f"{addresses_text(doc, 'to')}, {addresses_text(doc, 'cc')}"
    return (
        jsonvalue.get_str(doc.headers, "subject"),
        jsonvalue.get_str(doc.headers, "from"),
        recipients,
        doc.body,
        " ".join(attachment_names(doc)),
    )


def upsert(connection: sqlite3.Connection, doc: IndexDoc) -> None:
    """Insert or replace a message in a shard.

    Args:
        connection: The shard.
        doc: The message.
    """
    delete_doc(connection, doc.ulid)
    cursor = connection.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        message_row(doc),
    )
    connection.executemany(
        "INSERT INTO message_tags VALUES (?, ?)",
        [(doc.ulid, name) for name in doc.meta.tags],
    )
    connection.execute(
        "INSERT INTO fts (rowid, subject, from_addr, to_addrs, body,"
        " attachment_names) VALUES (?, ?, ?, ?, ?, ?)",
        (cursor.lastrowid, *text_row(doc)),
    )


Clause = tuple[str, Sequence[object]]


def text_clauses(query: Query) -> Sequence[Clause]:
    """Translate the free-text and address parts of a query.

    Args:
        query: The query.

    Returns:
        SQL conditions and their parameters.
    """
    clauses: list[Clause] = []
    if query.terms:
        match = " ".join(f'"{term}"' for term in query.terms)
        clauses.append(("fts MATCH ?", [match]))
    if query.sender:
        clauses.append(("m.from_addr LIKE ?", [f"%{query.sender}%"]))
    if query.recipient:
        pattern = f"%{query.recipient}%"
        clauses.append(("(m.to_addrs LIKE ? OR m.cc_addrs LIKE ?)", [pattern] * 2))
    return clauses


def filter_clauses(query: Query) -> Sequence[Clause]:
    """Translate the date, tag and attachment filters of a query.

    Args:
        query: The query.

    Returns:
        SQL conditions and their parameters.
    """
    clauses: list[Clause] = [("m.date >= ?", [query.after])]
    if query.before:
        clauses.append(("m.date < ?", [query.before]))
    tag_filter = "m.id IN (SELECT id FROM message_tags WHERE tag = ?)"
    clauses.extend((tag_filter, [name]) for name in query.tags)
    if query.has_attachments is not None:
        clauses.append(("m.has_attachments = ?", [int(query.has_attachments)]))
    return clauses


def where_clause(query: Query, exclude: Collection[str]) -> Clause:
    """Translate a query's filters into SQL.

    Args:
        query: The query.
        exclude: Message IDs to leave out (they are served from the tail).

    Returns:
        The WHERE clause and its parameters.
    """
    leave_out: Clause = (
        "m.id NOT IN (SELECT value FROM json_each(?))",
        [json.dumps(sorted(exclude))],
    )
    clauses = [leave_out, *text_clauses(query), *filter_clauses(query)]
    sql = " AND ".join(condition for condition, _ in clauses)
    return sql, [param for _, group in clauses for param in group]


def search_shard(
    connection: sqlite3.Connection, query: Query, exclude: Collection[str]
) -> Sequence[Hit]:
    """Run a query against one shard.

    Args:
        connection: The shard.
        query: The query.
        exclude: Message IDs to leave out.

    Returns:
        Ranked hits when the query has terms, else hits by date.
    """
    where, params = where_clause(query, exclude)
    ranked = bool(query.terms)
    snippet = (
        "snippet(fts, 3, '[', ']', '...', 12)"
        if ranked
        else f"substr(fts.body, 1, {SNIPPET_CHARS})"
    )
    score = "bm25(fts)" if ranked else "NULL"
    order = "bm25(fts)" if ranked else "m.date DESC, m.id DESC"
    rows = connection.execute(
        f"SELECT m.id, m.date, m.from_addr, m.subject, {snippet}, m.tags, {score}"
        " FROM messages m JOIN fts ON fts.rowid = m.rowid"
        f" WHERE {where} ORDER BY {order} LIMIT ?",
        (*params, query.limit),
    ).fetchall()
    return [row_hit(row) for row in rows]


def row_hit(row: Sequence[object]) -> Hit:
    """Convert a result row into a hit.

    Args:
        row: ``(id, date, from, subject, snippet, tags, score)``.

    Returns:
        The hit.
    """
    doc_id, date, sender, subject, snippet, tags, score = row
    return Hit(
        ulid=jsonvalue.as_str(doc_id),
        date=int(jsonvalue.get_number(dict(date=date), "date")),
        sender=jsonvalue.as_str(sender),
        subject=jsonvalue.as_str(subject),
        snippet=jsonvalue.as_str(snippet),
        tags=jsonvalue.string_list(
            dict(tags=json.loads(jsonvalue.as_str(tags))), "tags"
        ),
        score=None if score is None else jsonvalue.get_number(dict(s=score), "s"),
    )


def tag_counts(
    connection: sqlite3.Connection, exclude: Collection[str]
) -> Mapping[str, int]:
    """Count messages per tag in one shard.

    Args:
        connection: The shard.
        exclude: Message IDs to leave out.

    Returns:
        Message counts by tag.
    """
    rows = connection.execute(
        "SELECT tag, COUNT(*) FROM message_tags"
        " WHERE id NOT IN (SELECT value FROM json_each(?)) GROUP BY tag",
        (json.dumps(sorted(exclude)),),
    ).fetchall()
    return {str(tag): int(count) for tag, count in rows}
