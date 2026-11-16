#!/bin/bash
# Sourced, not run. A PostgreSQL URL split into the PG* variables libpq reads,
# so a script can hand a connection to psql or docker by variable name and keep
# the URL, and the password in it, off every command line.
#
#   . scripts/db_url_env.sh
#   db_url_to_pg_env "postgresql://user:p%40ss@host:6543/db?sslmode=require" || exit 1
#   db_url_display "$url"     # host:port/db, for a message; no user, no password
#
# Only %XX is decoded, as libpq decodes a URL: a backslash or any other byte of
# the password stays what it is (printf '%b' would turn "\t" into a tab).
# Query parameters other than sslmode are dropped; sslmode defaults to require.
# tests: scripts/test_db_url_env.sh

pct_decode() {
  local s="$1" out="" before hex ch
  while [[ "$s" == *%[0-9A-Fa-f][0-9A-Fa-f]* ]]; do
    before="${s%%%[0-9A-Fa-f][0-9A-Fa-f]*}"
    out+="$before"
    s="${s#"$before"}"
    hex="${s:1:2}"
    printf -v ch "\\x$hex"
    out+="$ch"
    s="${s:3}"
  done
  printf '%s' "$out$s"
}

db_url_to_pg_env() {
  local url="$1" rest creds hostpart hostport dbquery
  rest="${url#*://}"
  if [[ "$url" != *://* || "$rest" != *@* ]]; then
    echo "The connection string has no user and password (expected postgresql://user:password@host:port/db)." >&2
    return 1
  fi
  # The last @: a password that kept a raw @ still splits where the host begins.
  creds="${rest%@*}"
  hostpart="${rest##*@}"
  hostport="${hostpart%%/*}"
  dbquery="postgres"
  [[ "$hostpart" == */* ]] && dbquery="${hostpart#*/}"
  PGUSER="$(pct_decode "${creds%%:*}")"
  PGPASSWORD=""
  [[ "$creds" == *:* ]] && PGPASSWORD="$(pct_decode "${creds#*:}")"
  PGHOST="${hostport%%:*}"
  PGPORT=5432
  [[ "$hostport" == *:* ]] && PGPORT="${hostport##*:}"
  PGDATABASE="$(pct_decode "${dbquery%%\?*}")"
  [ -n "$PGDATABASE" ] || PGDATABASE=postgres
  PGSSLMODE=require
  if [[ "$dbquery" == *sslmode=* ]]; then
    PGSSLMODE="${dbquery##*sslmode=}"
    PGSSLMODE="${PGSSLMODE%%&*}"
  fi
  export PGUSER PGPASSWORD PGHOST PGPORT PGDATABASE PGSSLMODE
}

db_url_display() {
  local rest="${1#*://}"
  rest="${rest##*@}"
  printf '%s' "${rest%%\?*}"
}
