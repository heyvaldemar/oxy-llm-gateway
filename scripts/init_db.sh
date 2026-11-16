#!/bin/bash
# ==============================================================================
# 🐳 OXY ENGINE - GOLDEN SCHEMA INITIALIZER
# Destroys the `public` schema and reapplies db/schema.sql on Supabase
# (pooled or direct URLs). Dangerous: wipes data.
#
# Usage:
#   OXY_DB_URL="postgresql://..." bash scripts/init_db.sh --force
#   # or interactively (the URL is read without echo):
#   bash scripts/init_db.sh
#
# The URL, and the password in it, stays off every command line: it comes from
# OXY_DB_URL or the prompt, never as an argument, and reaches psql in the
# container as PG* environment variables passed by name (docker run -e NAME).
# ==============================================================================

set -euo pipefail

# --- STYLING ---
BOLD='\033[1m'
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BLUE='\033[0;34m'
NC='\033[0m'

# --- HELPERS ---
log_warn()  { echo -e "${YELLOW}⚠️  $1${NC}"; }
log_error() { echo -e "${RED}❌ $1${NC}"; }
log_info()  { echo -e "${BLUE}ℹ️  $1${NC}"; }

# --- ARG PARSING ---
FORCE=0
DB_URL_RAW=""

for arg in "$@"; do
  case "$arg" in
    -f|--force)
      FORCE=1
      ;;
    *)
      # A connection string as an argument would be on this script's command
      # line, where any user of the machine reads it with ps.
      log_error "Pass the connection string in OXY_DB_URL or at the prompt, not as an argument."
      exit 1
      ;;
  esac
done
DB_URL_RAW="${OXY_DB_URL:-}"

clear
echo -e "${CYAN}${BOLD}"
echo "   ██████╗ ██╗  ██╗██╗   ██╗    ██████╗ ██████╗ "
echo "  ██╔═══██╗╚██╗██╔╝╚██╗ ██╔╝    ██╔══██╗██╔══██╗"
echo "  ██║   ██║ ╚███╔╝  ╚████╔╝     ██║  ██║██████╔╝"
echo "  ██║   ██║ ██╔██╗   ╚██╔╝      ██║  ██║██╔══██╗"
echo "  ╚██████╔╝██╔╝ ██╗   ██║       ██████╔╝██████╔╝"
echo "   ╚═════╝ ╚═╝  ╚═╝   ╚═╝       ╚═════╝ ╚═════╝ "
echo -e "${NC}"
echo -e "${RED}${BOLD}!!! DANGER ZONE !!!${NC}"
echo "This script will WIPE the entire 'public' schema and recreate it."
echo "ALL DATA WILL BE LOST."
echo ""

if [ "$FORCE" -eq 1 ]; then
  log_warn "Running in NON-INTERACTIVE FORCE MODE (no prompts, no mercy) 💀"
fi

# --- PRE-FLIGHT CHECK ---
if [ ! -f "./db/schema.sql" ]; then
  log_error "CRITICAL ERROR: 'db/schema.sql' not found!"
  echo "You are running in 'Golden Schema' mode, but no schema file exists."
  exit 1
fi

# --- INPUT HANDLING ---
if [ -z "$DB_URL_RAW" ]; then
  if [ "$FORCE" -eq 1 ]; then
    log_error "No connection string provided and --force mode is enabled. Aborting."
    exit 1
  fi

  echo "Enter Supabase connection string (pooled OR direct, exactly as in dashboard):"
  read -s -p "> " DB_URL_RAW
  echo ""
  if [ -z "$DB_URL_RAW" ]; then
    log_error "No connection string provided. Aborting."
    exit 1
  fi
fi

# Trim whitespace/newlines just in case
DB_URL_RAW="$(echo "$DB_URL_RAW" | tr -d '[:space:]')"

# --- POOLER WARNING ---
if [[ "$DB_URL_RAW" == *".pooler.supabase.com"* ]]; then
  log_warn "Supabase pooler URL detected (.pooler.supabase.com)."
  log_warn "For heavy DDL (DROP SCHEMA ...) Supabase usually recommends the direct host (db.<project>.supabase.co:5432)."

  if [ "$FORCE" -eq 0 ]; then
    echo "If this URL worked for you before, you can still continue."
    read -p "Continue with this pooled URL anyway? [y/N]: " CONT
    case "${CONT:-n}" in
      [yY]*) ;;
      *) log_error "Aborted so you can grab a direct connection string from Supabase."; exit 1 ;;
    esac
  else
    log_warn "Force mode: continuing with pooled URL without additional prompts."
  fi
fi

# --- SSL NORMALIZATION ---
DB_URL="$DB_URL_RAW"
if [[ "$DB_URL" == *"sslmode="* ]]; then
  DB_URL_SSL="$DB_URL"
else
  if [[ "$DB_URL" == *"?"* ]]; then
    DB_URL_SSL="${DB_URL}&sslmode=require"
  else
    DB_URL_SSL="${DB_URL}?sslmode=require"
  fi
fi

# Mask password in URL for display
MASKED_URL="$(echo "$DB_URL_SSL" | sed -E 's#(postgres(ql)?://[^:]+):[^@]*@#\1:****@#')"

echo ""
echo "You are about to DESTROY and RE-INITIALIZE database:"
echo "  $MASKED_URL"
echo ""

if [ "$FORCE" -eq 0 ]; then
  read -p "Are you absolutely sure? Type 'DESTROY' to proceed: " CONFIRM
  if [ "$CONFIRM" != "DESTROY" ]; then
    log_error "Operation aborted."
    exit 1
  fi
else
  log_warn "Force mode: skipping 'DESTROY' confirmation and proceeding immediately."
fi

echo ""
echo -e "${BLUE}🚀 Launching Docker Container...${NC}"

START_TIME=$(date +%s)

# --- CONNECTION, OFF EVERY COMMAND LINE ---
# libpq reads PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE and PGSSLMODE from
# its environment, and docker passes each by name, so neither docker's nor
# psql's command line carries the URL or its password. The parts of the URL are
# percent-decoded as libpq would; query parameters other than sslmode are dropped.
url_decode() { printf '%b' "${1//%/\\x}"; }
rest="${DB_URL_SSL#*://}"
if [[ "$rest" != *@* ]]; then
  log_error "The connection string has no user and password (expected postgresql://user:password@host:port/db)."
  exit 1
fi
creds="${rest%%@*}"
hostpart="${rest#*@}"
hostport="${hostpart%%/*}"
dbquery="${hostpart#*/}"
[[ "$hostpart" == */* ]] || dbquery="postgres"
PGUSER="$(url_decode "${creds%%:*}")"
PGPASSWORD="$(url_decode "${creds#*:}")"
PGHOST="${hostport%%:*}"
PGPORT=5432
[[ "$hostport" == *:* ]] && PGPORT="${hostport##*:}"
PGDATABASE="$(url_decode "${dbquery%%\?*}")"
PGSSLMODE=require
if [[ "$dbquery" == *sslmode=* ]]; then
  PGSSLMODE="${dbquery##*sslmode=}"
  PGSSLMODE="${PGSSLMODE%%&*}"
fi
export PGUSER PGPASSWORD PGHOST PGPORT PGDATABASE PGSSLMODE

docker run --rm \
  -v "$(pwd)/db:/migrations" \
  -e PGUSER -e PGPASSWORD -e PGHOST -e PGPORT -e PGDATABASE -e PGSSLMODE \
  --dns 8.8.8.8 \
  --entrypoint /bin/sh \
  postgres:alpine \
  -c '
    set -e

    print_status() {
      printf "\033[0;36m   >> %s\033[0m\n" "$1"
    }

    print_status "🔥 Dropping & Recreating schema public..."
    psql -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"

    print_status "📜 Applying Golden Schema (schema.sql)..."
    psql -v ON_ERROR_STOP=1 -f /migrations/schema.sql

    print_status "🔄 Forcing PostgREST cache refresh (DDL cache buster)..."
    psql -c "CREATE TABLE IF NOT EXISTS _cache_buster (id int); DROP TABLE _cache_buster;"
    echo ""
  '

STATUS=$?

if [ $STATUS -eq 0 ]; then
  END_TIME=$(date +%s)
  DURATION=$((END_TIME - START_TIME))
  echo -e "${GREEN}✅ Database successfully initialized in ${DURATION}s!${NC}"
else
  log_error "Initialization FAILED (docker exit code: $STATUS)."
  exit $STATUS
fi