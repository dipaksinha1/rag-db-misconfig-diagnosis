"""PostgreSQL 16 documentation ingestion (T1).

``crawl`` finds and downloads the pages, ``parse`` turns them into chunks (one
per configuration parameter on the server-configuration pages, one or more per
section elsewhere) and ``cli`` is the ``pgdiag-ingest-docs`` entry point.
"""
