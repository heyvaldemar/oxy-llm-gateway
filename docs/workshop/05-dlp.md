# Module 5. DLP

**Time:** 10 minutes.
**You leave with:** four refusals for secrets, one of them for a secret in a tool definition, one request that passes on purpose, and a feel for what pattern matching catches.

DLP (data loss prevention) in Oxy scans every message, and the tool definitions sent with it, for the ten patterns you listed in Module 3. A match stops the request in the gateway, before the budget and before any provider.

The secrets below are made up on the spot with `openssl rand`, so no real token is ever typed or stored in this lab.

## 1. A secret the budget could not pay for

Your project's budget is still one cent from Module 4, and a `gpt-4o` request does not fit in it. Send one with a GitHub token in it:

```bash
oxy -s -w '\nHTTP %{http_code}\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"deploy with ghp_'"$(openssl rand -hex 18)"'"}]}'
```

**Expected output:**

```
{"error":{"code":"DLP_BLOCKED","message":"Request blocked: Sensitive data detected (github_token).","hint":"Remove the github_token from your message. The gateway checks for API keys, tokens, private keys and card numbers."}}
HTTP 451
```

451, not 402: that is guess **c**. DLP runs before the budget, so the gateway refuses the secret without asking whether the project could pay for it. `451 Unavailable For Legal Reasons` is the code Oxy uses for content it will not forward.

## 2. Raise the budget again

On the Control Plane, **Settings** → **Budget limit, total spend ($)** → `10.00` → **Save Changes**. The same `gpt-4o` request without the token now goes through at once:

```bash
oxy -s -o /dev/null -w 'HTTP %{http_code}\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"deploy"}]}'
```

**Expected output:**

```
HTTP 200
```

## 3. A secret, with money to spend

Now the budget has room. Send a GitHub token again (`openssl` makes a new one each time):

```bash
oxy -s -w '\nHTTP %{http_code}\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"deploy with ghp_'"$(openssl rand -hex 18)"'"}]}'
```

**Expected output:**

```
{"error":{"code":"DLP_BLOCKED","message":"Request blocked: Sensitive data detected (github_token).","hint":"Remove the github_token from your message. The gateway checks for API keys, tokens, private keys and card numbers."}}
HTTP 451
```

That is guess **b**: the budget makes no difference to DLP. The `hint` tells the caller what to remove.

A card number, in the usual groups of four (this one is the well-known test number, not a real card):

```bash
oxy -s -w '\nHTTP %{http_code}\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"charge 4111 1111 1111 1111 for the order"}]}'
```

**Expected output:**

```
{"error":{"code":"DLP_BLOCKED","message":"Request blocked: Sensitive data detected (credit_card).","hint":"Remove the credit_card from your message. The gateway checks for API keys, tokens, private keys and card numbers."}}
HTTP 451
```

## 4. A secret in a tool definition

An agent sends the gateway more than a prompt: a request can carry the definitions of the tools the model may call, and a definition is text that someone wrote by hand, sometimes with a credential pasted into the description so that the model "knows how to log in". DLP reads the name, the description and the parameters of every tool with the same ten patterns. The prompt below is clean; the token is in the tool:

```bash
oxy -s -w '\nHTTP %{http_code}\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"What is the deploy status?"}],"tools":[{"type":"function","function":{"name":"get_deploy_status","description":"Read the deploy status with the token ghp_'"$(openssl rand -hex 18)"'","parameters":{"type":"object","properties":{}}}}]}'
```

**Expected output:**

```
{"error":{"code":"DLP_BLOCKED","message":"Request blocked: Sensitive data detected (tool_github_token).","hint":"Remove the tool_github_token from your message. The gateway checks for API keys, tokens, private keys and card numbers."}}
HTTP 451
```

The same 451, and the name in the brackets now starts with `tool_`: the match was in a tool definition, not in a message, so whoever reads the refusal knows which part of the request to clean. The reason is recorded on the audit row as `tool_github_token`; Module 7 shows it.

## 5. What passes on purpose

An email address is not one of the ten patterns:

```bash
oxy -s -o /dev/null -w 'HTTP %{http_code}\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"reply to you@example.com"}]}'
```

**Expected output:**

```
HTTP 200
```

Oxy does not block an email address on its own, and the README's feature list says so. Anything else that does not look like one of the patterns passes too: a password typed in plain words, a token in a format the list does not know. DLP here catches well-known secret shapes; it is not a guarantee, and [`docs/security/dlp-checks.md`](../security/dlp-checks.md) says what it does not cover.

## Check yourself

- A teammate pastes a private key into a prompt by mistake. Which pattern catches it, and does the request reach the provider? (`private_key`, from its `-----BEGIN ... PRIVATE KEY-----` line. No: the gateway answers 451 itself.)
- A secret sits in a tool's description and the prompt is clean. Is the request refused, and how can you tell where the match was? (Yes, 451 `DLP_BLOCKED`; the pattern in the message starts with `tool_`, so it was found in a tool definition.)
- Someone sends a secret in a request the budget cannot cover. Which refusal do they get, and why? (451, because DLP runs before the budget.)

Next: [Module 6. The kill switch](06-kill-switch.md)
