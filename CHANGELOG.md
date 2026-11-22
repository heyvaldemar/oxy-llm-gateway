# Changelog

## [Unreleased]

### Added

- Local models are reachable. A request for `vllm/<name>`, `ollama/<name>`, `docker/<name>` or `local/<name>` answered 400 "Provider not configured" for want of a provider key the model never needed, so the adapters in `providers/` could not be reached from the gateway. A local provider now takes the organization's key if there is one, else `<NAME>_API_KEY`, else none, and a server started with its own key is sent it as a bearer token. `local/<name>` goes to `LOCAL_HTTP_BASE_URL` (Ollama's address when unset). A local model bills nothing per token unless `LOCAL_PROMPT_PRICE_PER_MILLION` and `LOCAL_COMPLETION_PRICE_PER_MILLION` say what a million tokens cost the operator; with them set, a project's budget caps it like a hosted model (`tests/test_local_models_keyless.py`).
- A proof for it, **local**, and two planted violations (`local_key`, `local_price`): requests for `vllm/...` are served by the recording fake with no provider key anywhere, a $0.02 budget buys exactly three at the configured price, none of eleven secret types sent to a local model reaches the server, and a killed session's next requests do not either.

### Fixed

- Every organization's request left with the first organization's provider key. The provider registry built one instance per provider name, and an instance keeps the key it was built with, so once one organization had called a provider, the others' requests were sent with that key and their own were never used. The registry now builds the instance for each call: constructors only set attributes and the HTTP client is shared, so there is nothing to cache, and no key is held between requests. The Gemini adapter still hands its key to Google's SDK through a process-wide setting (`genai.configure`), so concurrent Gemini requests of different organizations can cross; this does not change that, and its constructor, now run for each request, no longer logs a key prefix.

## [1.0.6] - 2026-11-22

Four defects that running the lab and reading the answers found, and three more of the same shape that fixing them turned up. Each has a test that fails on 1.0.5; the kill proof gained a check and a planted violation of its own.

### Fixed

- A secret that sits only in a tool definition is named. The request path kept the type of the first scan that found something with `or`, and a scan that finds nothing returns the string "none", which is truthy: a secret found only in `tools` or in the legacy `functions` was refused with 451 but described as "Sensitive data detected (none)", and the audit row and the log carried "none". It is now `tool_github_token` and the like, and the log counts every match, not only the messages' (`tests/test_dlp_tools_only.py`).
- Deleting an agent no longer touches another organization. `DELETE /v1/agents/{id}` marked the agent's running sessions killed by agent id alone, so the owner of one organization could end another's sessions by sending that agent's id; the delete itself was scoped and found nothing, but the sessions were already killed. It now answers 404 for an agent that is not the caller's organization's. The sessions it ends record who, when and why (`kill_requested_by`, `kill_requested_at`, `kill_reason` were empty on this path) and get the kill flag in Redis the gateway reads first. `DELETE /v1/mcp/servers/{id}` had the same shape: it deleted the server's tools by server id before looking at the organization (`tests/test_agent_delete.py`).
- Deleting an agent or an MCP server answered 500 after the rows were gone. The engine's database client did not ask PostgREST for the deleted rows and read the empty reply as JSON; it now does, so `.data` holds what was removed and a delete that removed nothing is a 404.
- `GET /cost-optimizer/recommendations` and `/usage-analysis` asked the `subscriptions` table for an `organization_id` column it has never had (a subscription belongs to a user), so PostgREST answered 400 and the routes answered 500 for everyone. The organization now comes from the caller's membership, as on every other dashboard route, and a caller in no organization gets 403. The plan-tier gate on these two routes is gone: 1.0.1 took plan tiers out of the request path, and `/v1/cost-optimizer` is not gated either (`tests/test_cost_optimizer_legacy_org.py`).
- The API description told an agent to "wait for reset" on a 402. A project's budget is its total spend and nothing resets it; the response's own hint, "do not retry until it changes", was right and the description now says the same (`tests/test_openapi_402.py`).

### Proofs

- The kill proof reads the killed session's row: `killed`, who, when, why (`manual_kill`) and the end time must all be there. A new planted violation, `kill_record`, makes the row forget who killed it and why, and the proof must fail. The lab's 1.0.1 run had found those fields empty after a kill; the routes have written them since, and this keeps them written.

## [1.0.5] - 2026-11-16

The dashboard says what the engine does. Four of these changes are Tatiana Mikhaleva's pull requests (#32 to #35): a page that promised a monthly limit, alerts at other thresholds or a cap no tier has now says what the gateway enforces, and the API Keys page shows when a key was last used.

### Added

- The API Keys page shows when each key was last used (#33). Nothing wrote `api_keys.last_used_at` up to 1.0.4. The gateway now writes it in a background task the request does not wait for, at most once a minute per key (a Redis `SET NX` with a 60-second expiry), so a busy key costs one database write a minute. Created and Last used show in the reader's time zone. The smoke test requires the time to reach the page.

### Changed

- Every long-running service in both compose files comes back after a reboot or a crash (`restart: unless-stopped`), and Redis, and Postgres in the development stack, get 30 seconds to write their data out before Docker kills them, instead of the default 10 (#34).
- The release workflow checks the exact signer: `release-assets.yml` on the ref the run ran on, not a prefix any workflow of the repository matches. Its changed-archive check requires each tool to refuse in its own words, so Sigstore out of reach or a missing tool no longer passes it, and a run started by hand runs from `main` only.
- `pnpm-workspace.yaml` lifts glob to 14, minimatch to 11 and webpack to 5.112.2, past GHSA-3g3m-2fjr-6pw4, GHSA-g3hv-75wg-m249, GHSA-8fgc-7cc6-rx7x and GHSA-38r7-794h-5758 (#37). All four sit in build-time tooling that `@sentry/nextjs` 10 and typescript-eslint pull in; none reaches the running services. minimatch 3.1.5, which the ESLint plugins need, is below its advisory's range and stays.

### Fixed

- Budget alerts reach the organization's Slack, Discord and Zapier webhooks. `check_budget_and_alert`, which the request path calls, did not hand the organization to the function that sends, so that branch never ran for a real request; its test called the inner function directly and missed it. The 24-hour "already sent" mark for a threshold is now set only when a channel took the alert: the Slack and Discord call set it whatever happened, so once the organization was passed, a failed email would have kept that threshold quiet for a day. `tests/test_budget_alerts_notifications.py` covers the request path, the mark, and the post to the stored Slack webhook. (#36)
- A project's budget is its total spend, and the dashboard says so (#32). Project Settings, the Budget Oracle and the budget tooltip spoke of a monthly limit that resets; nothing resets it, and once it is spent requests answer 402 until it is raised.
- The Budget Oracle promised alerts at 75, 90 and 100%; the engine sends them at 80, 95 and 100% of the budget (#35).
- No plan tier caps requests since 1.0.1, yet a 429 opened a "Monthly request cap reached" notice and a "Budget Exceeded" toast (#35). A 429 is a rate limit and a spent budget answers 402: the notice and its component are gone, and the toast says to wait and try again. The Plan limits page, the landing's budget card and the screenshot list no longer name a request cap.

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
