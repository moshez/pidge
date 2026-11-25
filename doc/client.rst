The pidge_client library
========================

``pidge_client`` is a client for pidge's MCP endpoint that uses only the
Python standard library (3.12 or newer). It lives in ``client/`` in the pidge
repository as a separate distribution, ``pidge-client``; nothing needs
installing.

Quick start
-----------

1. Get a token. Through the MCP connection, call ``get_session_token``. It
   returns ``token`` (valid for 5 minutes), ``expires_at`` and ``endpoint``.

2. Clone the repository and put the client on ``PYTHONPATH``:

   .. code-block:: shell

       git clone https://github.com/moshez/pidge
       export PYTHONPATH=$PWD/pidge/client/src

3. Script against the endpoint. Results stay in the script; print only what
   you need:

   .. code-block:: python

       import datetime
       import sys

       from pidge_client import SearchArgs, Session, iter_search, views
       from pidge_client import calls, workflows

       session = Session(endpoint=sys.argv[1], token=sys.argv[2])

       # Every inbox message, following next_cursor page by page.
       hits = list(iter_search(session, SearchArgs(tags=["inbox"])))
       now = datetime.datetime.now().astimezone()
       with open("inbox.html", "w") as out:
           out.write(views.as_page("Inbox", views.render_inbox(hits, now)))

       # One message, with its original HTML body.
       message = calls.get_message(session, hits[0].id)
       body = workflows.fetch_body_html(session, message)
       print(views.render_message(message, body, now.tzinfo))

   A token expires after 5 minutes; a call with an expired one raises
   ``AuthError``, and a fresh ``get_session_token`` call fixes it.

Layers
------

``pidge_client.transport``
   ``Session(endpoint=..., token=...)`` and ``call_tool``. A failed tool
   raises ``ToolError``, a JSON-RPC error ``RpcError``, an HTTP 401
   ``AuthError``, any other HTTP error (404 included) ``HTTPStatusError``.
   A ``Session`` sends ``initialize`` before its first request; pidge keeps
   no MCP sessions, so there is no session ID. Replies may be plain JSON or
   server-sent events (``refresh_tools``); notifications in a stream are
   ignored. The transport is pluggable; the default uses
   ``urllib.request``.

``pidge_client.calls``
   One typed wrapper per tool: ``search(session, SearchArgs(...))`` returns a
   ``Page`` of ``Hit`` objects and a ``next_cursor``; ``get_message``,
   ``tag_email``, ``list_tags``, ``get_attachment`` (a URL),
   ``send_email(session, Draft(...))``, ``get_session_token`` and
   ``refresh_tools`` (every server tool with a one-sentence summary, and the
   deployed build). Models
   are parsed (``models.parse_*``) from the exact JSON the tools return, so
   they load from saved JSON files too.

``pidge_client.workflows``
   ``iter_search`` follows ``next_cursor`` in date order;
   ``fetch_body_html`` downloads a message's ``0-body.html`` through its
   presigned URL (a plain GET with no Authorization header); ``archive``
   removes the ``inbox`` tag.

``pidge_client.views``
   Pure renderers: ``render_inbox(messages, now, tz)``,
   ``render_message(message, body_html, tz, page=False)`` and
   ``as_page(title, fragment)``. Fragments use only CSS variables and load
   nothing external, so they display in Claude's inline widget. With
   ``page=True`` the original HTML is framed in a sandboxed iframe instead.
   Tracking pixels (images 2px or less on a side) are dropped, and runs of
   tiny link-only blocks, like 0-10 survey scales, collapse onto one line.

   The views also run from the command line on saved tool output (one
   object, a list, or a ``{"results": [...]}`` search response):

   .. code-block:: shell

       python -m pidge_client.views inbox hits.json --tz America/Los_Angeles
       python -m pidge_client.views --page message msg.json --html body.html

Development
-----------

Run ``nox`` in ``client/``. The tests run every typed call through pidge's
own ``handle_rpc`` in-process, check the hard-coded tool signatures against
the server's ``tools/list``, and compare rendered fixtures with golden files
(``PIDGE_UPDATE_GOLDEN=1`` rewrites them). ``nox -s e2e`` checks the client
against a deployed endpoint; set ``PIDGE_ENDPOINT`` and ``PIDGE_TOKEN`` from a
``get_session_token`` result first.
