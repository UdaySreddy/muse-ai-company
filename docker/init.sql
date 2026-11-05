-- AI Team OS — PostgreSQL initialization script
-- Automatically executed by docker-entrypoint-initdb.d

-- Vector search extension (pgvector)
CREATE EXTENSION IF NOT EXISTS vector;

-- Trigram fuzzy search extension
CREATE EXTENSION IF NOT EXISTS pg_trgm;
