pidge
=====

A personal mailbox on AWS with an MCP interface, for one person and their
AI agent.

* Mail for a domain arrives through SES receiving into S3; one Lambda
  function parses it, stores it, and indexes it in per-year SQLite FTS5
  shards kept in the same bucket.
* The same function serves a stateless MCP endpoint (a Lambda Function URL)
  with six tools: ``search``, ``get_message``, ``get_attachment``,
  ``list_tags``, ``send_email`` and ``tag_email``. It is an OAuth 2.1
  resource server; Cognito (email codes, dynamic client registration) signs
  you in.
* Sending goes through a transactional outbox in S3 and SES v2.
* Nobody gets paged: a reconciler runs every 15 minutes, fixes what it can,
  and mails the rest into the inbox tagged ``ops``.

Layout
------

``src/pidge/``
    The Lambda code (``pidge.lambda_main.lambda_handler``) and its tests.
``Dockerfile``
    The Lambda container image.
``infra/bootstrap/``
    One-time stack: state bucket, GitHub OIDC deploy role, ECR repository.
``infra/main/``
    Everything else: S3, Lambda, SES, EventBridge, Cognito, Cloudflare DNS.
``e2e/``
    End-to-end tests: the built image against moto, with S3 notifications,
    Lambda retries, SES events, injected AWS errors and a movable clock.
``infra/setup.py``
    The one-time setup script.
``infra/scripts/``
    Cognito helpers for settings the AWS provider cannot manage yet.
``.github/workflows/image.yml``
    Builds the image and runs the smoke and end-to-end tests on every push.
``.github/workflows/deploy.yml``
    Pushes that tested image and applies the main stack on every push to ``trunk``.

Cost guardrail
--------------

A monthly budget emails at 50% and at 100% forecast. At 100% actual, a kill
switch stops SES receiving and the function. At 200%, a budget action denies
the function's and the deploy role's IAM permissions. See
`RECOVERY.md <RECOVERY.md>`_.

Setup
-----

See `docs/SETUP.md <docs/SETUP.md>`_ for the one-time setup (one script, infra/setup.py).

Development
-----------

.. code-block:: sh

    pip install nox
    nox            # tests (100% branch coverage), lint, mypy, docs
    nox -s build && docker buildx build --platform linux/amd64 --load -t pidge:candidate .
    nox -s e2e     # the image against moto (needs Docker)
