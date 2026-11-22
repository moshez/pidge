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
``infra/scripts/``
    Cognito helpers for settings the AWS provider cannot manage yet.
``.github/workflows/deploy.yml``
    Builds the image and applies the main stack on every push to ``trunk``.

Setup
-----

See `docs/SETUP.md <docs/SETUP.md>`_ for the one-time manual steps.

Development
-----------

.. code-block:: sh

    pip install nox
    nox            # tests (100% branch coverage), lint, mypy, docs
