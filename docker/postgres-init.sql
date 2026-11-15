-- Needed for sql_spy's server-side timing (also requires
-- shared_preload_libraries=pg_stat_statements, set in docker-compose.yml).
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
