#!/bin/bash
set -euo pipefail

# Oxy Dev Menu
# One place to run core tests, DB reset, and Gateway healthcheck.
# Usage:
#   sh scripts/oxy_dev_menu.sh

cd "$(dirname "$0")/.."

pause() {
  echo ""
  read -p "Press Enter to return to the menu..." _
}

run_core_tests() {
  if ! sh scripts/test_core.sh; then
    echo "Core tests failed."
  fi
}

run_smoke() {
  if [ -z "${OXY_API_KEY:-}" ]; then
    echo "ERROR: OXY_API_KEY is not set. Export your dev key before running the smoke test."
    return
  fi
  if ! sh scripts/dev_smoke.sh; then
    echo "Smoke test failed."
  fi
}

run_quick() {
  if [ -z "${OXY_API_KEY:-}" ]; then
    echo "ERROR: OXY_API_KEY is not set. Export your dev key before running the quick check."
    return
  fi
  if ! sh scripts/dev_quick.sh; then
    echo "Quick dev check failed."
  fi
}

run_full() {
  if [ -z "${SUPABASE_URL:-}" ]; then
    echo "ERROR: SUPABASE_URL is not set. Export a Supabase connection string before running the full check."
    return
  fi
  echo "DANGER: This will wipe the public schema via init_db.sh --force."
  if ! sh scripts/dev_full.sh; then
    echo "Full dev check failed."
  fi
}

run_integration_tests() {
  if ! sh scripts/test_integration.sh; then
    echo "Integration tests failed (or prerequisites not met)."
  fi
}

run_init_db_interactive() {
  if ! sh scripts/init_db.sh; then
    echo "init_db.sh (interactive) failed."
  fi
}

run_init_db_force() {
  if [ -z "${SUPABASE_URL:-}" ]; then
    echo "ERROR: SUPABASE_URL is not set. Export a Supabase connection string before running --force."
    return
  fi
  if ! OXY_DB_URL="$SUPABASE_URL" bash scripts/init_db.sh --force; then
    echo "init_db.sh --force failed."
  fi
}

run_ui_lint() {
  if ! pnpm ui:lint; then
    echo "UI lint failed."
  fi
}

run_ui_test() {
  if ! pnpm ui:test; then
    echo "UI tests failed."
  fi
}

run_ui_check() {
  if ! pnpm ui:check; then
    echo "UI lint+tests failed."
  fi
}

while true; do
  clear
  cat <<'MENU'
========================================
Oxy Dev Menu

  1. Run core engine tests
  2. Run Oxy Gateway smoke (healthcheck)
  3. Quick dev check (tests + smoke)
  4. Full dev check (DB reset + tests + smoke) [DANGER]
  5. Golden Schema init_db.sh (interactive)
  6. Golden Schema init_db.sh --force (non-interactive, uses SUPABASE_URL)
  7. Run engine integration tests
  8. UI — Lint (eslint)
  9. UI — Tests (vitest)
 10. UI — Lint + Tests
  0. Exit
========================================
MENU
  read -p "Choice: " choice
  echo ""
  case "${choice}" in
    1)
      run_core_tests
      echo "Done: core tests."
      pause
      ;;
    2)
      run_smoke
      echo "Done: smoke."
      pause
      ;;
    3)
      run_quick
      echo "Done: quick dev check."
      pause
      ;;
    4)
      run_full
      echo "Done: full dev check."
      pause
      ;;
    5)
      run_init_db_interactive
      echo "Done: init_db.sh (interactive)."
      pause
      ;;
    6)
      run_init_db_force
      echo "Done: init_db.sh --force."
      pause
      ;;
    7)
      run_integration_tests
      echo "Done: engine integration tests."
      pause
      ;;
    8)
      run_ui_lint
      echo "Done: UI lint."
      pause
      ;;
    9)
      run_ui_test
      echo "Done: UI tests."
      pause
      ;;
    10)
      run_ui_check
      echo "Done: UI lint + tests."
      pause
      ;;
    0)
      echo "Goodbye."
      exit 0
      ;;
    *)
      echo "Invalid choice."
      pause
      ;;
  esac
done

