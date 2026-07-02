#!/bin/sh
# Runs once on first Postgres boot (docker-entrypoint-initdb.d).
# Creates the separate database n8n uses for its own tables, so application
# data (llamacag) and n8n internals stay apart.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE n8n OWNER "$POSTGRES_USER";
EOSQL
