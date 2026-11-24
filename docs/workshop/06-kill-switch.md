# Module 6. The kill switch

**Time:** 10 minutes.
**You leave with:** an agent, one of its sessions killed, and the difference between stopping a session and stopping a key.

An agent sends many requests in a row on its own. If one of its runs goes wrong, you want to stop that run now, without breaking every other run and every other service that uses the same key. In Oxy that run is an agent session, and the kill switch stops it.

## 1. Sign in from the terminal

Creating an agent and starting a session are an owner's actions, made with a signed-in session rather than with an API key. Your browser has such a session; your terminal gets one the same way, through a magic link. Use the address you signed in with in Module 1.

```bash
ANON_KEY=$(docker compose config --format json | jq -r .services.ui.environment.NEXT_PUBLIC_SUPABASE_ANON_KEY)
curl -s -X POST localhost:8080/auth/v1/otp -H "apikey: $ANON_KEY" \
  -H 'Content-Type: application/json' -d '{"email":"you@example.com"}'; echo
```

**Expected output:**

```
{}
```

GoTrue sent a new magic link to Mailpit. Instead of clicking it, follow it with `curl` and keep the session token it redirects to:

```bash
LINK=$(curl -s localhost:8025/api/v1/message/latest | jq -r .Text | grep -o 'http://[^ ]*/auth/v1/verify[^ ]*')
TOKEN=$(printf 'url = "%s"\n' "$LINK" | curl -s --config - -o /dev/null -w '%{redirect_url}' | grep -o 'access_token=[^&]*' | cut -d= -f2)
oxy_owner() { printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | curl --config - "$@"; }
echo "session ${TOKEN:0:12}..."
```

**Expected output:**

```
session eyJhbGciOiJI...
```

The magic link carries a one-time token, so it reaches curl on standard input too, and `oxy_owner` is `oxy` with the signed-in session instead of the key. The session is valid for one hour; if a later command answers 401, run these two blocks again.

## 2. An agent and a session

```bash
PROJECT_ID=$(oxy_owner -s localhost:8000/management/projects | jq -r '.[0].id')
AGENT_ID=$(oxy_owner -s localhost:8000/v1/agents \
  -H 'X-CSRF-Token: workshop' -b csrf_token=workshop \
  -H 'Content-Type: application/json' \
  -d '{"name":"workshop-agent","project_id":"'"$PROJECT_ID"'","default_model":"gpt-4o-mini"}' \
  | jq -r .agent.id)
SESSION_ID=$(oxy_owner -s -X POST localhost:8000/v1/agents/$AGENT_ID/sessions \
  -H 'X-CSRF-Token: workshop' -b csrf_token=workshop \
  -H 'Content-Type: application/json' -d '{}' | jq -r .session.id)
echo "agent   $AGENT_ID"
echo "session $SESSION_ID"
```

**Expected output:**

```
agent   4fa03277-7f64-47bf-b51e-1abbd6f8f684
session 4eab6ea5-1cce-4f9e-aa37-dcfa323739d9
```

The `X-CSRF-Token` header and the `csrf_token` cookie carry the same value. The engine asks for that pair on every change made with a signed-in session, so that a page on another site cannot make the change through your browser. Any value works as long as the two match.

## 3. A request in the session

An agent tells Oxy which session a request belongs to with two headers:

```bash
oxy -s -o /dev/null -w 'HTTP %{http_code}\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "X-Oxy-Agent-Id: $AGENT_ID" -H "X-Oxy-Session-Id: $SESSION_ID" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"step 1"}]}'
```

**Expected output:**

```
HTTP 200
```

## 4. Kill it

The kill switch takes the project's API key (`oxy`), so whatever watches the agent (a person, a script, an alert) can pull it without signing in:

```bash
oxy -s -w '\nHTTP %{http_code}\n' -X POST localhost:8000/v1/agent-sessions/$SESSION_ID/kill
```

**Expected output:**

```
{"status":"ok"}
HTTP 200
```

The agent's next step:

```bash
oxy -s -w '\nHTTP %{http_code}\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "X-Oxy-Agent-Id: $AGENT_ID" -H "X-Oxy-Session-Id: $SESSION_ID" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"step 2"}]}'
```

**Expected output:**

```
{"error":{"code":"session_terminated","message":"Agent session 4eab6ea5-1cce-4f9e-aa37-dcfa323739d9 has been terminated. Create a new session with POST /v1/agents/{agent_id}/sessions; do not retry with this session_id."}}
HTTP 410
```

`410 Gone`, and a message an agent can act on: this session is over, retrying it will not help. That answers guess **d** from Module 3.

Now guess **e**, a secret in a killed session:

```bash
oxy -s -w '\nHTTP %{http_code}\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "X-Oxy-Agent-Id: $AGENT_ID" -H "X-Oxy-Session-Id: $SESSION_ID" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"deploy with ghp_'"$(openssl rand -hex 18)"'"}]}'
```

**Expected output:**

```
{"error":{"code":"session_terminated","message":"Agent session 4eab6ea5-1cce-4f9e-aa37-dcfa323739d9 has been terminated. Create a new session with POST /v1/agents/{agent_id}/sessions; do not retry with this session_id."}}
HTTP 410
```

Still 410: the kill switch is step 4 of the flow you printed in Module 3, and DLP is step 5. The request never got as far as being scanned.

Kill it a second time:

```bash
oxy -s -w '\nHTTP %{http_code}\n' -X POST localhost:8000/v1/agent-sessions/$SESSION_ID/kill
```

**Expected output:**

```
{"detail":"Session not found or already stopped"}
HTTP 404
```

## 5. A session is not a key

Send the same request without the two session headers:

```bash
oxy -s -o /dev/null -w 'HTTP %{http_code}\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"step 2"}]}'
```

**Expected output:**

```
HTTP 200
```

The kill switch stopped one session, as it should: other sessions and other services that share the key keep working. It is also why [`docs/agents/kill-switch.md`](../agents/kill-switch.md) calls it best-effort: an agent that leaves the headers out, or calls a provider directly, is not stopped by it. To stop everything a key can do, revoke the key. Module 7 ends with that.

## 6. In the dashboard

Open **Audit Log** (<http://localhost:3000/dashboard/audit>). The kill is there as `agent_session.killed`, with `api_key` as the actor and the session id in its details. The two refused requests are not on this page: it lists changes made by people and keys, while requests have their own trail, which Module 7 reads.

## Check yourself

- Your agent's run is stuck in a loop and you have the key, not a browser session. Which one call stops it? (`POST /v1/agent-sessions/{id}/kill` with the key.)
- Why does the second kill answer 404 and not 200? (The session is no longer running, so there is nothing left to stop.)

Next: [Module 7. The audit row](07-audit-row.md)
