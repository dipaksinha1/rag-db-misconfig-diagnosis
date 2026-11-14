"""pgsql-performance mailing-list ingestion (T3): skipped by decision of the project owner.

postgresql.org's ``robots.txt`` disallows, for every user agent, the archive's
listings and bulk downloads (``/list/``, ``/message-id/raw/``,
``/message-id/flat/``, ``/message-id/mbox/``, ``/search/``). There is no
permitted way to discover or download the threads, and the project respects
``robots.txt`` strictly. So this package is empty, no ``mailing_list.jsonl`` is
produced, and the RAG index is built from the docs and the release notes only.
The ``mailing_list`` source stays in the chunk schema (``pgdiag.chunks``) but
no chunk uses it. See ``PROGRESS.md``.
"""
