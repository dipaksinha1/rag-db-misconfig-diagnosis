"""pgdiag: does RAG help an LLM diagnose PostgreSQL 16 misconfigurations?

Two separate databases are involved and must not be confused:

* the RAG index: a local knowledge base built from PostgreSQL 16 docs, release
  notes and pgsql-performance threads (``pgdiag.ingest``, ``pgdiag.index``);
* the test database: a PostgreSQL 16 container that gets one setting broken per
  case (``pgdiag.db``, ``pgdiag.workload``, ``pgdiag.faults``, ``pgdiag.collect``).
"""

__version__ = "0.1.0"
