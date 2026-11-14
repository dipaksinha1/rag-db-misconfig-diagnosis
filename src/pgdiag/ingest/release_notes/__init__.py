"""PostgreSQL 16 release notes ingestion (T2).

``parse`` reads the list of release pages from the appendix index and turns
each page (16.0 and every 16.x minor release) into chunks: one per release-note
item or small group of short items, plus the Overview and Migration prose.
``cli`` is the ``pgdiag-ingest-release-notes`` entry point.
"""
