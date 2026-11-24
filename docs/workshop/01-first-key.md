# Module 1. From `git clone` to the first key

**Time:** 15 minutes, most of it the first build.
**You leave with:** the stack running, a user, an organization with one project, and an Oxy API key in your shell.

## 1. Clone and start

```bash
git clone https://github.com/heyvaldemar/oxy-llm-gateway.git
cd oxy-llm-gateway
git checkout --quiet v1.0.6
docker compose up -d --build --wait
```

The first run builds the engine and dashboard images; it took about two minutes when this lab was tested and takes longer on a slow connection. `--wait` returns only when every service reports healthy.

**Expected output** (the last lines):

```
 Container oxy-llm-gateway-engine-1 Healthy
 Container oxy-llm-gateway-ui-1 Healthy
 Container oxy-llm-gateway-worker-budget-1 Healthy
 ...
```

See what is running:

```bash
docker compose ps --format 'table {{.Service}}\t{{.Status}}'
```

**Expected output:**

```
SERVICE                STATUS
engine                 Up 11 seconds (healthy)
gotrue                 Up 16 seconds
mailpit                Up 22 seconds (healthy)
nginx-postgrest        Up 16 seconds (healthy)
postgres               Up 22 seconds (healthy)
postgrest              Up 16 seconds
redis                  Up 22 seconds (healthy)
ui                     Up 5 seconds
worker-anomaly         Up 5 seconds (healthy)
worker-budget          Up 5 seconds (healthy)
worker-cost-anomaly    Up 5 seconds (healthy)
worker-intelligence    Up 5 seconds (healthy)
worker-logs            Up 5 seconds (healthy)
worker-webhook-retry   Up 5 seconds (healthy)
```

Fourteen services: the gateway (`engine`) and its six workers, the dashboard (`ui`), Postgres with PostgREST and GoTrue for data and sign-in, nginx in front of those two, Redis, and Mailpit, which catches the sign-in mail so that nothing is sent out. The diagram in the [README](../../README.md#architecture) shows how they connect.

Ask the gateway how it is:

```bash
curl -s localhost:8000/health
```

**Expected output:**

```json
{"status":"healthy","version":"1.0.6","timestamp":"2026-11-23T15:46:40.155879+00:00","redis":"healthy","supabase":"healthy"}
```

## 2. Sign in with a magic link

1. Open <http://localhost:3000/login>, type `you@example.com` (any address works) and press **Send magic link**.
2. Open Mailpit at <http://localhost:8025>. The message **Your Magic Link** is there. Open it and press **Log In**.

The link signs you in and opens the onboarding page. Nothing left your machine: GoTrue sent the mail to Mailpit, which keeps it locally.

## 3. Onboarding

The onboarding page asks for a name and for at least one provider key.

1. **Full Name:** anything.
2. **OpenAI API Key:** paste this placeholder:

   ```
   sk-test-oxy-workshop-placeholder-not-a-real-key
   ```

3. Leave the other fields empty and press **Complete Setup**.

Why a placeholder: a provider key that starts with `sk-test` puts your organization in Oxy's mock mode. The gateway accepts it without calling OpenAI, and later runs every check on each request (the key, the kill switch, DLP, the budget) and then answers the request itself instead of calling a provider. The prefixes are listed as `MOCK_KEY_PREFIXES` in [`packages/engine/routes/proxy.py`](../../packages/engine/routes/proxy.py). With a real provider key the same checks run, and the request then goes to the provider.

You land on the Control Plane: a project called **Default Project** with a budget of **$10.00** and one key, created during onboarding, of which only the prefix is shown.

## 4. Your first key

Oxy shows a key in full only once, when it is created, so make one you can copy:

1. On the Control Plane, press **Generate Key**.
2. The panel **API Key Generated!** shows the key, starting with `oxy_live_`. Press the copy button next to it.

Put the key in your shell without echoing it or leaving it in the shell history: run the first line, paste the key, press Enter.

```bash
read -rs OXY_API_KEY && export OXY_API_KEY
echo "${OXY_API_KEY:0:13}..."
```

**Expected output:**

```
oxy_live_646a...
```

One habit before the first request. A key written on curl's command line, in an `Authorization` header passed with `-H`, can be read by any user of the machine with `ps` while curl runs. Give curl the key on its standard input instead, through a small function you use for the rest of the lab:

```bash
oxy() { printf 'header = "Authorization: Bearer %s"\n' "$OXY_API_KEY" | curl --config - "$@"; }
```

`oxy` is `curl` with your key added from standard input (`--config -`); every other flag works as it does with curl. The repository's own end-to-end test, `scripts/stack_smoke.sh`, passes its tokens the same way.

## 5. The first answer

Oxy has a built-in health check: a fixed system prompt and the word `ping` get an answer from the gateway itself, before any provider is chosen.

```bash
oxy -s localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4.1-mini","messages":[{"role":"system","content":"You are a healthcheck bot for the Oxy Gateway."},{"role":"user","content":"ping"}]}' \
  | jq -r '.choices[0].message.content'
```

**Expected output:**

```
pong from Oxy Gateway
```

Now a request that takes the whole path, with every check:

```bash
oxy -s localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello"}]}' \
  | jq -r '.choices[0].message.content'
```

**Expected output:**

```
🧪 Universal Mock Response

Provider: openai
Model: gpt-4o-mini
Key Prefix: sk-test

This is a test response. No external API was called.
```

The request body is the OpenAI chat-completions format, and the answer is too, so any OpenAI SDK can talk to Oxy by changing its base URL to `http://localhost:8000/v1` and its key to your Oxy key.

## Check yourself

- Which service would you look at if the magic link never arrived? (Mailpit at <http://localhost:8025> first, then `docker compose logs gotrue`.)
- Your key is in `$OXY_API_KEY` and nowhere else. What happens to it if you close this terminal? (It is gone from the shell. Generate a new one in the dashboard; the old one keeps working until you revoke it.)

Next: [Module 2. The dashboard](02-dashboard.md)
