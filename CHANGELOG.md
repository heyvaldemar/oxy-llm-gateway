# Changelog

## [Unreleased]

### Fixed

- Budget alerts reach the organization's Slack, Discord and Zapier webhooks. `check_budget_and_alert`, which the request path calls, did not hand the organization to the function that sends, so that branch never ran for a real request; its test called the inner function directly and missed it. The 24-hour "already sent" mark for a threshold is now set only when a channel took the alert: the Slack and Discord call set it whatever happened, so a failed email could have kept that threshold quiet for a day. `tests/test_budget_alerts_notifications.py` covers the request path, the mark, and the post to the stored Slack webhook.

## [1.0.4] - 2026-11-16

No secret on a command line. A gateway that sells DLP and an audit trail kept its own Redis password and vault key where any user of the host could read them with `ps`. Found by the fleet's conformance check.

### Security

- The Redis password stood on the command line of four health checks (`redis.from_url('redis://:<password>@…')` in two workers, in both compose files) and of the production Redis check (`redis-cli -a <password>`), each run every few seconds. The workers' checks read `REDIS_URL` from their environment; Redis's check passes the password through `REDISCLI_AUTH` and counts only `PONG` (`redis-cli ping` exits 0 on NOAUTH and on a wrong password too, so the old development check passed against a Redis it could not log in to).
- The vault key stood on the postmaster's command line (`postgres -c app.vault_key=…`) for as long as Postgres ran. It now reaches Postgres through `postgresql.auto.conf`: on the first boot `db/00-vault-key.sh` writes it from `SUPABASE_VAULT_KEY`, and on every later boot `db/postgres-entrypoint.sh` does the same, so a database made before 1.0.4 keeps its key. awk reads the key from its environment, so it is on no command line (`ALTER SYSTEM` refuses a custom parameter).
- The documentation's `curl` examples put the Oxy API key on curl's command line (`-H "Authorization: Bearer $OXY_API_KEY"`). They pass it on curl's standard input (`--config -`) now, through one shell function; the quickstart reads the key with `read -s`.
- The production Caddy, Prometheus and Grafana services have resource limits, like the others.

### Proofs

- `scripts/argv_secrets.py` renders every compose stack with a random value planted in each secret-named variable and fails when one lands in a command, an entrypoint or a health check; on 1.0.3's compose files it finds all twelve. Its self-test plants the 1.0.3 forms back and must catch each. On the running stack it freezes Redis, runs every health check by hand so each stays in flight, and samples the host's `ps` five times a second for every secret the stack was given; a canary in a container's command line must be seen first. CI also requires the vault key to be the configured one after the first boot and after a restart, set once.

## [1.0.3] - 2026-11-15

The audit trail keeps its promise: every request made with a valid key has a `request_logs` row, refusals included, and revoking a key no longer erases its rows. Found while writing the workshop lab.

### Fixed (audit)

- Revoking a key in the dashboard erased its audit trail. `DELETE /management/key/{id}` deleted the key, and `request_logs.api_key_id` referenced it with `ON DELETE CASCADE`, so every row the key had made, refusals included, went with it. The key is now revoked (`is_active` false, `deactivated_reason` "revoked"): it stops at once, stays listed as inactive, and keeps its rows. The foreign key is `ON DELETE SET NULL`, so a delete made by hand keeps the rows too. An existing database keeps the old rule until it runs: `ALTER TABLE request_logs DROP CONSTRAINT request_logs_api_key_id_fkey, ADD CONSTRAINT request_logs_api_key_id_fkey FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE SET NULL;`
- A served request's row did not name the key that made it (nor its agent and session): the served path wrote through a wrapper that took none of them, so only refusals were attributed to a key. Every row carries its key now.
- Requests refused by the burst limit (429), by a suspended organization (403) or for a body that is not JSON (400) left no audit row; each now has one, without the prompt (it was never read). The gateway's built-in health ping is audited too.
- The audit proof covers both: it revokes a key through the dashboard's route and requires the same number of rows for that key before and after, and checks that the foreign key does not cascade; it sends requests the real burst limiter refuses and counts their rows. Two new planted violations, the 1.0.2 revoke (a `DELETE` with the cascade) and a 429 without its row, must each make it fail: twelve in all.

### Fixed (dashboard)

- Threats Blocked counted every request: served requests are recorded as "safe", and anything but "none" counted as a threat. Only what DLP or the stream scanner finds counts now; refusals by the budget, the kill switch, the burst limit or a suspended organization are not threats.

## [1.0.2] - 2026-11-15

A fix release: the dashboard's budget and project buttons work, deleting a project keeps its audit trail, and Dependabot keeps the dependencies current on its own.

### Fixed (dashboard)

- The dashboard can set a project's budget, rename, create and delete a project again. All four answered 500 in v1.0.1, because `PUT`, `POST` and `DELETE /management/project` called `fn_update_project`, `fn_create_project` and `fn_delete_project`, which the schema never had (creating a project also read a field its request does not carry, and asked `subscriptions` for a `tier` column that is called `plan_tier`). They now write the `projects` table directly, after the same owner-or-admin check, and setting a budget drops the project's cached keys so the next request reads the new cap.
- Deleting a project deactivates it instead of erasing it. Its keys stop at once and it leaves every listing and the plan's project count; no route finds it again, so no new key can be made for it, and `fn_validate_api_key` refuses the keys of a deleted project (a fresh database; an existing one keeps the old function until `db/schema.sql` is applied again). Its request logs stay: `request_logs` references the project with `ON DELETE CASCADE`, so a real `DELETE` would have taken the audit trail with it.
- Disconnecting the provider keys in Settings works (`fn_disconnect_provider` did not exist either): the organization's keys are marked inactive, and the gateway reads only active ones. Adding or disconnecting a provider key now needs an owner or admin, as other organization settings do since 1.0.1.
- `GET /management/export-data` answers 501 instead of 500: its database function does not exist, and the README lists it as not wired.
- `tests/test_schema_functions.py` reads every database function the engine calls and fails when `db/` does not create it, outside an explicit list of not-wired features. The smoke test now sets a budget, deletes a project (and requires its key to stop) and creates one, all with the dashboard's session.
- `docs/security/dlp-checks.md` claimed emails were checked and suggested `sk-test-123` as a test secret; the code checks neither. It now lists the patterns `DLP_PATTERNS` holds, and the 451 hint no longer mentions SSNs or PII.

### Dependencies

- The UI's unit tests run on Vitest 4 (4.1.12, with `@vitest/coverage-v8` and `@vitest/ui` at the same version and Vite 7.3). Coverage names source files only (`src/lib/**/*.{ts,tsx}`, `src/hooks/**/*.{ts,tsx}`): Vitest 4 reports every file `coverage.include` matches, and `src/lib` holds a Markdown guide. Dependabot checks the pnpm workspace from its root alone; listing `packages/ui` as well opened a second pull request for each update, without the lockfile.
- Dependabot proposes patch and minor updates only, once a release is seven days old, grouped per ecosystem, and `dependabot-automerge.yml` merges them once CI (the proofs and the devcontainer included) and CodeQL pass for the commit and every dependency moved is stated as patch or minor. Majors are held back until 2027-02-15; the README's "Dependencies" section lists them.
- UI: `@supabase/ssr` 0.12, `lucide-react` 0.577, `eslint-config-next` 16.4.5, `critters` 0.0.25. Engine development tools: higher lower bounds for `pytest`, `pytest-asyncio`, `ruff` and `fakeredis`; Dependabot now moves a lower bound only when the new version falls outside it.
- `redis` stays on 5.2. Dependabot's grouped update to 5.3.1 failed the fail-closed proof: with Redis paused, the refused requests had no answer within 60 s instead of a 503 within a second. Measured outside the gateway, ten concurrent commands against a paused Redis take 1 s on 5.2.1 and 6 to 10 s on 5.3.1.

### Fixed

- Eight navigations to the app's own pages went through `window.location.href` (the error pages, the offline page, the playground, the SSO panel, sign-out); they use the Next router, which `eslint-config-next` 16.4 requires. The error card's Dashboard button clears the error before it navigates.
- The devcontainer's proxy could start before the dashboard's container existed and stop with `host not found in upstream "ui"`, failing the devcontainer job at random; it now waits for that container to start.

## [1.0.1] - 2026-11-15

### License

- `LICENSE` is the Apache License 2.0 text, unchanged. Before, it opened with a copyright header and two of its sections (6, Trademarks, and 9, Accepting Warranty) were reworded, so GitHub could not recognise the license. The copyright moves to `NOTICE`: Tatiana Mikhaleva and Vladimir Mikhalev.

### Behaviour

- No plan tier caps requests any more. Organizations on the `free` tier were refused after 100,000 requests in a calendar month (`403 free_plan_limit_reached`), a quota left from the hosted service; budgets are the spend control. Tiers still set project, agent and team-member caps. `tests/test_request_quota.py` sends a request for an organization far past that count and requires it to be served.
- The dashboard's API Keys page works: it lists, creates and revokes a project's keys with the signed-in session through `/management`, instead of sending the session to `/v1/api-keys`, which takes an Oxy API key and refused it.
- An agent session's detail no longer fails on its timeline: it filtered `request_logs` by a column that does not exist (`agent_session_id`; the column is `session_id`).

- The dashboard's pages work with the signed-in session. Fifteen `/v1` routes the dashboard reads (audit log, requests, attribution, billing overview, guardrails, notifications, webhooks, organization, privacy, providers, setup, intelligence, export, cost optimizer, prompts, team) took only an Oxy API key, so every one of those pages answered 401; they now also accept the session of an active member of the organization, and the gateway itself still takes keys only. The smoke test calls every route the dashboard reads with a session.
- Organization settings change only by an owner or admin of the organization: privacy, guardrails, providers, the organization's profile, notification settings, webhooks and team invitations. Before, any member's Oxy API key could change them (a key acts as the member who created it), and with the dashboard's session any signed-in member could have. A developer or viewer now gets 403, and so does everyone when the role cannot be read (503). `tests/test_org_settings_roles.py` sends a developer, a viewer and a non-member to each of the nine routes that change these settings; the smoke test saves guardrails as the owner.
- `/organizations` returned an empty list for everyone (it read a nested field that is merged in), `/v1/agents` failed with 500 (the PostgREST client had no `range`), and `/v1/guardrails`, `/v1/requests` and parts of `/v1/intelligence` failed (calls not awaited, request parameters not typed). Saving notification settings a second time no longer collides with the first row.
- The dashboard's CSV export uses `/v1/exports/requests.csv`; `/api/v1/reports/export` read a table that does not exist and is removed.
- Another organization can no longer stop a session through `DELETE /v1/agents/{agent_id}/sessions/{session_id}`: the kill flag was set before the organization was checked.
- Cached responses are kept per organization: the cache key left the organization out, so one organization could be answered from another's cache.
- `/auth/sso/callback` answers 501: it took the user id and email from the request body without verifying the identity provider (harmless only because the database function it needs is missing). Accepting a team invitation answers 501 as well (its database function is missing too), and the dashboard no longer offers invitations. The README lists what else is not wired in this release.
- `GET /public/features`, a marketing list that claimed per-agent budgets and local models, is removed; `GET /public/status` checks nothing and says so.

### Tests

- Every offline test runs in CI, 427 in all (one more skips itself without a live key), not a hand-picked list. The tests that failed on stale fakes are fixed; new tests cover the dashboard session, the cross-organization kill, the per-organization cache and the owner-or-admin check on organization settings, each also run against the old code to see it fail.

### Documentation

- The in-app docs say what the code does: claims without a code path behind them are corrected or removed.
- The README says which CI run its proof numbers and the walkthrough recording come from.
- Two co-authors and maintainers on equal terms: the README's Authors section and `MAINTAINERS.md`.

## [1.0.0] - 2026-11-15

Oxy is a self-hosted LLM gateway: a FastAPI engine with an OpenAI-compatible `/v1/chat/completions` endpoint, a Next.js dashboard, and Postgres with PostgREST and GoTrue for data and sign-in. It puts budgets, DLP, a kill switch, audit logs and cost attribution in front of OpenAI, Anthropic, Gemini and local models. It is an Apache 2.0 reference implementation.

### Getting started

- `docker compose up` starts the whole stack from a clean clone with no `.env` file, cloud account or provider key. `packages/engine/.env` and `packages/ui/.env.local` are optional overrides.
- Sign-in works on the local stack: magic-link mail lands in Mailpit at <http://localhost:8025>.
- Stripe is optional. Without `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` only the endpoints that call Stripe answer 503; plan limits and usage still come from Postgres.
- Development ports are published on 127.0.0.1 only, and third-party images are pinned by digest.

### Proofs

- The README's main claims are measured end to end against a recording fake provider: budgets are never overspent (also across the budget worker's sync cycles and after Redis loses the spend counter), DLP keeps secrets from reaching the provider, the kill switch stops a running agent, another tenant's agent and session ids are refused, a revoked key stops at once, requests the gateway cannot check without Redis are refused, the browser cannot reach the engine's database functions or an open table policy, no tenant API key is stored in Redis, and every request is audited, refusals included.
- CI runs the proofs, then plants the violation each proof exists to catch and requires that proof to fail.
- A `status` branch (`.github/workflows/status.yml`) records the last 30 days of CI runs on main and the latest proofs, in the same shape as the other fleet repositories.

### Try it

- `.devcontainer/` opens the whole stack in GitHub Codespaces (or any devcontainer host) with the recording fake provider from `proofs/` in place of a model provider. Each start makes a fresh demo organization, API key and agent session; nothing is stored in the repository. The dashboard, auth and the engine share one forwarded port. CI builds the devcontainer and runs the smoke test and the README's three requests inside it.

### Security and behaviour

- Database functions are executable by `postgres` and `service_role` only; the anon key and signed-in browser sessions can no longer call them. Row-level security is on for the admin audit log, notification settings, monthly usage and webhooks.
- DLP detects GitHub, Slack and Google API tokens in addition to the existing key, JWT, private-key and card-number patterns.
- The kill switch works: `POST /v1/agent-sessions/{id}/kill` now stops the session at the gateway, and only a member of the session's organization can kill it.
- Requests refused for budget, kill switch or suspension are recorded in `request_logs`, so the audit trail covers refusals as well as completions.
- A budget is now the project's total spend. The budget worker used to move the Redis spend counter into Postgres every 60 seconds (`GETDEL`), which handed every project its whole budget again once a minute; it now copies the counter, and a counter Redis has lost is reloaded from that copy instead of starting from zero. `projects.current_spend` keeps micro-dollars (`NUMERIC(18, 6)`), and Redis evicts only keys that expire (`volatile-lru`), never the spend counters.
- Clearing a project's cache (on a budget or project change) no longer deletes its spend counter, and it now reaches every cached key of the project, not only keys made by `/management/keys`.
- Revoking a key through `/v1/api-keys` drops its cached record at once; before, a revoked key kept working for up to 24 hours. Cached key records now live 5 minutes.
- Redis holds no tenant API key in plain text: the cache, its lock and the per-project key sets are named by the key's SHA-256.
- `X-Oxy-Agent-Id` and `X-Oxy-Session-Id` are checked against the API key's organization; before, any tenant could write another tenant's ids into its own request logs. A killed session is refused from its row in Postgres even when its Redis kill flag has expired or is gone.
- Fail closed: a budget that cannot be checked is refused with 503, an unreadable kill flag is never read as "not killed", the Redis client has timeouts, and the broken Postgres fallback for budget reservations is gone. The subscription lookup on every request is removed from the request path.
- PostgREST's database password (`authenticator`) is generated on each install's first boot instead of being a constant in `db/supabase-compat.sql`; `AUTHENTICATOR_PASSWORD` sets it explicitly.
- The browser roles can no longer insert into `sso_audit_log` or `user_achievements`.
- Public text says what the code does: budgets are per project (the agent budget fields are not enforced), the in-app budget page lists only the refusal the gateway gives (402, or 503 when the budget cannot be checked), and `GET /public/status` no longer reports services it did not check.

### Removed

- The `leads` table (anyone with the public anon key could insert into it) and the add-on purchase endpoints, which charged through Stripe without turning the add-on on. Add-ons are feature flags an operator sets in `organization_add_ons`; `GET /add-ons/catalog?organization_id=` shows them.

- Growth features left from the hosted service: prompts pushing a larger plan and the worker that sent them, the annual-billing and fast-checkout flows, lead scoring, the unauthenticated `/public/metrics` endpoint (organization and sign-up counts for a landing page), and UI experiments with their conversion tracking.
- Plan prices. Plan tiers (`free`, `starter`, `growth`, `business`, `enterprise`) are quota levels an operator assigns in the database; see `docs/getting-started/free-tier.md`.
