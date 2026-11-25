API Reference
=============

.. automodule:: pidge
   :members:

The ``search`` tool
-------------------

``search`` takes ``query``, ``from``, ``to``, ``after``, ``before``,
``tags``, ``has_attachments`` and ``limit`` (1 to 100), all optional,
plus two arguments that control ordering and paging:

``order``
   ``"relevance"`` ranks hits by score; ``"date"`` lists them newest first
   by ``(date, id)``, using the query words only as a filter. The default
   is ``"relevance"`` when ``query`` has words, else ``"date"``.

``cursor``
   The ``next_cursor`` of a previous date-order response. Pass it back with
   the same other arguments to get the next page. It is an opaque string
   (the base64url of ``{date}:{id}`` of the last hit returned). A cursor
   with relevance order, or a malformed cursor, is a tool error.

The result is ``{"results": [...], "next_cursor": ...}``. Each result has
``id``, ``date``, ``from``, ``subject``, ``snippet``, ``tags`` and
``ranked``. ``next_cursor`` is a string when a date-order search has more
results, and ``null`` otherwise. Relevance order is not pageable, because
scores are computed per yearly index shard and change when shards are
rewritten.

Paging is by keyset, not offset: a message's date and id never change, so
paging neither skips nor repeats messages when they are indexed or retagged
between pages.
