#!/bin/bash
# Tests for scripts/db_url_env.sh. Every value here is made up for the test.
#   bash scripts/test_db_url_env.sh
set -uo pipefail
cd "$(dirname "$0")/.."
. scripts/db_url_env.sh

fails=0
check() {  # check NAME EXPECTED ACTUAL
  if [ "$2" == "$3" ]; then
    printf 'ok    %s\n' "$1"
  else
    printf 'FAIL  %s: expected [%q], got [%q]\n' "$1" "$2" "$3"
    fails=$((fails + 1))
  fi
}

# A backslash and %40 in the password: only %40 is decoded. Up to 1.0.4 the
# decoder was printf '%b', which turned the "\t" into a tab.
db_url_to_pg_env 'postgresql://user.proj:a\tb%40c%5Cd%@pooler.example.com:6543/postgres?sslmode=require&pgbouncer=true'
check "backslash kept, %40 and %5C decoded, a lone % kept" 'a\tb@c\d%' "$PGPASSWORD"
check "user with a dot" "user.proj" "$PGUSER"
check "host" "pooler.example.com" "$PGHOST"
check "port" "6543" "$PGPORT"
check "database without the query" "postgres" "$PGDATABASE"
check "sslmode from the query, other parameters dropped" "require" "$PGSSLMODE"

db_url_to_pg_env 'postgres://me:plain@db.example.com/oxy'
check "default port" "5432" "$PGPORT"
check "default sslmode" "require" "$PGSSLMODE"
check "plain password" "plain" "$PGPASSWORD"

db_url_to_pg_env 'postgresql://me:x@db.example.com:5433/oxy?sslmode=disable'
check "sslmode disable" "disable" "$PGSSLMODE"

db_url_to_pg_env 'postgresql://me:p%C3%A4ss@db.example.com/oxy'
check "UTF-8 from two encoded bytes" "päss" "$PGPASSWORD"

db_url_to_pg_env 'postgresql://me:a%25b@db.example.com/oxy'
check "%25 is a percent sign, decoded once" "a%b" "$PGPASSWORD"

db_url_to_pg_env 'postgresql://me:a@b@db.example.com/oxy'
check "a raw @ in the password: split at the last @" "a@b" "$PGPASSWORD"
check "a raw @ in the password: the host" "db.example.com" "$PGHOST"
check "display with a raw @ in the password shows none of it" "db.example.com/oxy" "$(db_url_display 'postgresql://me:a@b@db.example.com/oxy')"

if db_url_to_pg_env 'postgresql://db.example.com/oxy' 2>/dev/null; then
  check "a URL without a user is refused" "refused" "accepted"
else
  check "a URL without a user is refused" "refused" "refused"
fi

check "display: no user, no password, no query" "pooler.example.com:6543/postgres" \
  "$(db_url_display 'postgresql://user.proj:a%40b@pooler.example.com:6543/postgres?sslmode=require')"

[ "$fails" -eq 0 ] && echo "all passed" || echo "$fails failed"
exit "$fails"
