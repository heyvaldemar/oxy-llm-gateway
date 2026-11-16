# DEV NOTES (Oxy Gateway / Engine)

Practical checklist for core contributors.

## One-time setup
- Clone repo: `git clone https://github.com/heyvaldemar/oxy-llm-gateway.git && cd oxy-llm-gateway`
- Fastest path: `docker compose up` — runs the full stack (Postgres + PostgREST + GoTrue + nginx + Redis + engine + UI) locally, no cloud accounts required.
- For host-side Python work (tests, scripts): `python3 -m venv .venv && .venv/bin/pip install -r packages/engine/requirements.txt`
- For host-side UI work: `cd packages/ui && pnpm install`

## Environment variables
- `SUPABASE_URL` — Postgres connection. Default (local stack): `http://nginx-postgrest:8080`. For hosted: `https://<project>.supabase.co`.
- `SUPABASE_SERVICE_KEY` — service-role JWT signed with `SUPABASE_JWT_SECRET`.
- `OXY_API_KEY` — Dev API key for gateway smoke (seeded by schema.sql for local dev).
- Optional: `OPENAI_API_KEY`, `OPENAI_API_BASE`, `REDIS_PASSWORD`, Stripe/etc. as per service needs.
- Plan tiers: levels the operator assigns per org in `organizations.subscription_plan` (default `free`). They set project, agent and team-member caps (`PLAN_LIMITS` in `routes/billing.py`, the project caps in `main.py`); no tier caps requests. See `docs/getting-started/free-tier.md`.
- Intelligence: `GET /v1/intelligence/overview` surfaces anomalies, cost insights, forecast, and a heuristic security score (based on guardrails/DLP/rate limits). Forecast is best-effort, not a billing guarantee.
- Exports/Audit: `GET /v1/exports/requests.csv` (params: from, to, model, provider, status, limit<=50k) returns request logs as CSV for audit. Retention duration is an operational policy (`request_log_retention_days`, 7–2555 days).
- Cost attribution: `GET /v1/attribution/summary` groups spend by cost attribution tags (customer/team/feature). Params: from, to, key, limit (max 100). Default range = month-to-date.
- Prompt Library: `/v1/prompts` (list/create) plus GET/PATCH/DELETE by id. List supports search/tag/limit. Org-scoped via member authors + verified prompts. UI at `/dashboard/prompts`.
- Cost Optimizer: `GET /v1/cost-optimizer/recommendations` (window 7d/30d, limit<=50). Heuristic model-switch/caching/throttling savings; estimates only. UI at `/dashboard/optimizer`.
- Privacy & retention: Org fields `privacy_mode` (`zero_prompt_logging`, `metadata_only`, `debug_full`), `request_log_retention_days` (7–2555, default 365). API: `GET/POST /v1/privacy`. Retention worker deletes old `request_logs`. UI: `/dashboard/privacy`.
- Audit log: Table `admin_audit_logs`; helper `core.audit.log_admin_event`; events for guardrails/privacy updates. API `GET /v1/audit/logs` with limit/event_type/since/until. UI: `/dashboard/audit`.
- Billing (optional Stripe): `POST /billing/create-checkout-session`, `POST /billing/webhook`, `GET /billing/subscription-status`; `GET /v1/billing/overview` reads plan and usage from Postgres and works without Stripe. Without `STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET` the Stripe endpoints answer 503. Env: `STRIPE_PRICE_ID_*`. UI at `/dashboard/billing`.
- Add-ons: per-org feature flags in `organization_add_ons` (`sso`, `white_label`, `audit_logs`, `extra_project`, `dedicated_support`); see `docs/features/add-ons.md`.
- Referral credits: `/management/credits/*`; amounts from `REFERRAL_CREDIT_AMOUNT_USD` and `REFERRAL_MAX_PER_REFERRER_USD`; see `docs/features/referral-credits.md`.
- Setup Wizard: `GET /v1/setup/status` returns org setup steps (providers, guardrails, privacy, billing; SSO optional). UI at `/dashboard/setup`.
- API Keys: Dashboard endpoints `GET /v1/api-keys` (metadata), `POST /v1/api-keys` (returns secret once), `DELETE /v1/api-keys/{id}` (revoke). Org scoped via projects. UI at `/dashboard/api-keys` with quickstart snippets for `/v1/chat/completions`.
- Playground: `/dashboard/playground` calls `/v1/chat/completions` (authenticated fetch) to try prompts and view tokens/latency/JSON. Good first 5 minutes after creating an API key.
- Team & roles: `GET /v1/team` (members + invites), `POST /v1/team/invites`, `DELETE /v1/team/invites/{id}`. UI at `/dashboard/team` for members list, pending invites, and invite form.
- Agents & sessions: `GET /v1/agent-sessions`, `GET /v1/agent-sessions/{id}`, `POST /v1/agent-sessions/{id}/kill` (uses fn_kill_agent_session). UI `/dashboard/agents` with status filters and per-session kill.
- Notifications & webhooks: `GET/POST /v1/notifications/settings`, `GET/POST/DELETE /v1/webhooks`. Tables `notification_settings`, `webhooks`. UI `/dashboard/notifications` to manage budget/anomaly email alerts and webhook integrations.
- Org settings & branding: `GET/POST /v1/org` (display_name, slug, logo_url, timezone, locale, support_email, billing_email). UI `/dashboard/settings/org`.
- Provider settings: `GET/POST /v1/providers/config` (enabled flags, env-detected). UI `/dashboard/providers`. Setup step “providers” completes when any provider is enabled.

## Daily flows
- Changed DB schema (local stack)?
  - `docker compose down -v && docker compose up -d postgres` — wipes the volume and re-applies `db/supabase-compat.sql` + `db/schema.sql` cleanly.
  - `make test-core` — verifies offline business logic still passes.
- Changed DB schema (hosted Supabase)?
  - `OXY_DB_URL="postgresql://..." bash scripts/init_db.sh --force` — DANGER: wipes the public schema.
  - `make test-core`
- Refactored engine logic?
  - `make test-core` — 258 offline tests, sub-second on M-series Macs.
  - `make dev-quick` (requires engine running) — adds smoke against `/v1/chat/completions`.

## EPIC: Feature Gaps

### P0
- [P0][kill-switch] Add UI/e2e tests and verify best-effort kill path messaging aligns with behavior.

### P1
- [P1][integrations-hubspot] Add integration tests for HubSpot sync; validate config flow.
- [P1][integrations-slack-discord] GA: Slack/Discord alerts best-effort; maintain tests/docs.
- [P1][integrations-zapier] GA: Zapier outbound triggers; maintain tests/docs/UI.
- [P1][agent-templates] Add UI/e2e coverage for templates; validate backend enforcement/config.
- [P1][referral-credits] Add referral-specific tests.
- [P1][addons-sso] GA; add automated gating tests for SSO/SAML flows.
- [P1][addons-white-label] GA; add tests for branding suppression/domain gating.
- [P1][addons-audit-logs] GA; add tests for extended retention gating.
- [P1][addons-dedicated-support] GA; flag-only label—add regression tests.
- [P1][cost-optimizer] Keep outputs conservative; monitor heuristics.
- [P1][dlp-checks] Ensure deployments validate regex patterns; best-effort only.
- [P1][anomaly-detection] Validate anomaly/forecast outputs; config-dependent.
- [P1][cost-spike-auto-suspend] Validate thresholds and best-effort suspend behavior.
- [P1][budgets] Monitor enforcement/config; defaults are defaults, not commitments.
- [P1][geo-blocking] Validate configuration and effectiveness per deployment. (Promoted to GA; maintain env configuration and best-effort messaging.)
- [P1][intelligence] Validate heuristic score/projections with enabled controls.
- [P1][free-tier] Keep backend enforcement aligned with the documented default limits.

### P2
- [P2][founders] Keep the authors section to facts that can be checked.

- Verify gateway via curl (healthcheck fast-path):
  ```bash
  export OXY_API_KEY="oxy_live_..."
  curl http://localhost:8000/v1/chat/completions \
    -H "Authorization: Bearer $OXY_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{
      "model": "gpt-4.1-mini",
      "messages": [
        {"role": "system", "content": "You are a healthcheck bot for the Oxy Gateway. Always respond with exactly: pong from Oxy Gateway."},
        {"role": "user", "content": "ping"}
      ]
    }'
  ```
  Expect assistant content: `pong from Oxy Gateway`.

## Scripts & targets
- DB reset: `OXY_DB_URL="$SUPABASE_URL" bash scripts/init_db.sh --force` (DANGER: wipes public)
- Core tests: `make test-core` or `sh scripts/test_core.sh`
- Full sanity (reset + tests + smoke): `make dev-full`
- Quick check (tests + smoke, no DB reset): `make dev-quick`
- Interactive dev menu: `make dev-menu` or `sh scripts/oxy_dev_menu.sh`
 - Integration tests: `make test-integration` or `sh scripts/test_integration.sh`

## Notes
- Healthcheck fast-path never calls external LLMs; safe to run often.
- Assume backend running at `http://localhost:8000` for smoke/dev checks.

## Dev menu and test layout
- Dev menu: `scripts/oxy_dev_menu.sh` (make target: `dev-menu`) provides shortcuts to core tests, smoke, quick/full checks, and Golden Schema reset.
- Engine tests:
  - Core (offline): `scripts/test_core.sh` / `make test-core`
    - files: test_dlp, test_budget_logic, test_workers, test_routes, test_providers, test_middleware, test_core_logic, test_billing_utils, test_healthcheck, test_billing_gate
  - Integration (live services, running engine): `scripts/test_integration.sh` / `make test-integration`
    - files: test_chat_completions_smoke, integration/gamma_billing_sim.py, integration/omega_simulation.py
  - Additional: load (Locust) under `packages/engine/tests/load`; not core or integration.
  - Legacy/experimental tests should move to `packages/engine/tests/_archive/` with a note.
- UI tests:
  - Current active: `packages/ui/tests/e2e/smoke.spec.ts`
  - Archive location: `packages/ui/tests/_archive/` for legacy/experimental cases.

## Test strategy
- Core engine: offline, no network; run always/CI-safe.
- Integration engine: live services (OpenAI, Supabase, Stripe), requires running engine @ http://localhost:8000 and env vars (OXY_API_KEY, OPENAI_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY, STRIPE_WEBHOOK_SECRET).
- Gateway smoke: `scripts/dev_smoke.sh` uses the healthcheck fast-path (pong from Oxy Gateway).
- Dev menu: `make dev-menu` wraps core, smoke, quick/full checks, integration, init_db.
- Load: `packages/engine/tests/load/locustfile.py` (separate Locust run).
- Pytest warnings: we narrowly ignore the slowapi `asyncio.iscoroutinefunction` deprecation (Python 3.14+, third-party) until slowapi upgrades.

## Feature surface (landing = code = tests = docs)
Canonical feature matrix: `docs/product/FEATURE_MATRIX.yml` (single source of truth; no alternates).

- Statuses (`ga`, `beta`, `planned`) live in `docs/product/FEATURE_MATRIX.yml`. "GA" here means implemented in code, surfaced in the dashboard, and covered by docs — not an availability commitment.

- Implemented and surfaced in the dashboard/landing: kill-switch; budgets; cost-spike-auto-suspend; loop-detection; response-caching; cost-optimizer (heuristic); cost-attribution & CSV export; privacy-modes; privacy-logging; dlp-checks; anomaly-detection; response-audit-logs; key-rotation; rate-limiting-ddos; security-headers; geo-blocking (best-effort); cookie-banner; integrations-webhooks; integrations-hubspot; integrations-slack-discord; integrations-zapier; agent-templates; intelligence; security-score; local-llm (OpenAI-compatible, no SLA); referral-credits (usage-only); free-tier; addons-sso; addons-white-label; addons-audit-logs; addons-dedicated-support; addons-extra-projects.

- Implemented but not on the landing (dashboard surfaces only): setup-status (Setup Wizard + `/v1/setup/status` advisory-only); api-keys; playground; team-and-roles.

- Beta/preview: none.

- Planned/coming soon: none.

- CI expectations: run `pytest`, `pnpm test`, `make validate-features`, and `make lint-features` (matrix + SLA-wording lint). All GA items must keep code_refs valid, have unit + integration coverage where applicable, and have docs with setup, usage, and caveats; no SLAs.

See `docs/product/FEATURE_MATRIX_GUIDE.md` for the step-by-step workflow when adding or changing a feature.

Quick release checklist (landing = matrix = code = tests = docs):
- `python3 scripts/validate_feature_matrix.py`
- `python3 scripts/lint_sla_language.py` (or `make lint-features`)
- `pytest` (or `make test-core` / `make dev-quick`) — ensure new/changed features have coverage
- `pnpm test` (UI) when UI is touched
- Confirm landing copy matches matrix/product_contract and uses heuristic/non-SLA language where applicable
