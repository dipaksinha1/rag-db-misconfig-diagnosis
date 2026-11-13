-- Runs once, when the data volume is first initialised (docker-entrypoint-initdb.d).
-- The library itself is loaded via shared_preload_libraries in postgresql.conf.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
