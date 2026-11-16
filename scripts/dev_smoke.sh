#!/bin/bash
set -euo pipefail

# Oxy Gateway dev smoke test (healthcheck fast-path).
#
# Usage:
#   export OXY_API_KEY=oxy_live_...
#   sh scripts/dev_smoke.sh

API_URL="${API_URL:-http://localhost:8000}"

if [ -z "${OXY_API_KEY:-}" ]; then
  echo "ERROR: OXY_API_KEY is not set. Export the dev key before running this script." >&2
  exit 1
fi

payload=$(cat <<'JSON'
{
  "model": "gpt-4.1-mini",
  "messages": [
    {
      "role": "system",
      "content": "You are a healthcheck bot for the Oxy Gateway. Always respond with exactly: pong from Oxy Gateway."
    },
    { "role": "user", "content": "ping" }
  ],
  "stream": false
}
JSON
)

# The key reaches curl on standard input (--config -), not on its command line.
response=$(printf 'header = "Authorization: Bearer %s"\n' "${OXY_API_KEY}" | curl -s -X POST "${API_URL}/v1/chat/completions" \
  --config - \
  -H "Content-Type: application/json" \
  -d "${payload}")

echo "${response}"

# Optional assertion: ensure assistant reply is exactly the expected pong
assistant_content=$(python3 - <<'PY'
import json, sys
try:
    data = json.loads(sys.stdin.read())
    choices = data.get("choices") or []
    if not choices:
        sys.exit(1)
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if content == "pong from Oxy Gateway":
        sys.exit(0)
    sys.exit(2)
except Exception:
    sys.exit(3)
PY
<<< "${response}")

status=$?
if [ $status -ne 0 ]; then
  echo "Healthcheck assertion failed: assistant content was not 'pong from Oxy Gateway'." >&2
  exit 1
fi

echo "✅ Dev smoke succeeded."

