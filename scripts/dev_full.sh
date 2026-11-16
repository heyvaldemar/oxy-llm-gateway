#!/bin/bash
set -euo pipefail

# Full dev sanity: Golden Schema reset + core tests + gateway smoke.
# DANGER: init_db.sh wipes the public schema.
#
# Usage:
#   SUPABASE_URL="postgresql://..." sh scripts/dev_full.sh

cd "$(dirname "$0")/.."

SUPABASE_URL="${SUPABASE_URL:-postgresql://postgres.USER:password@aws-1-us-east-1.pooler.supabase.com:6543/postgres}"

echo "🧨 Re-initializing Supabase schema from Golden Schema..."
echo "   Using SUPABASE_URL=${SUPABASE_URL}"
OXY_DB_URL="$SUPABASE_URL" bash scripts/init_db.sh --force

echo "🧪 Running engine tests..."
sh scripts/test_core.sh

echo "ℹ️  Make sure the engine is running on http://localhost:8000 before running this script."
echo "🚦 Running Oxy Gateway dev smoke..."
sh scripts/dev_smoke.sh

