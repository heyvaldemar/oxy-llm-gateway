# Oxy workshop: from `git clone` to a refused request

A hands-on lab of about 80 minutes. You start Oxy on your laptop with one `docker compose up`, get your first key, find your way around the dashboard and the docs, and then watch the gateway refuse requests for four reasons: a request the budget cannot cover, a secret in the prompt, a killed agent session and a revoked key. Each refusal leaves a row in the audit trail, and you read those rows at the end.

No LLM provider account and no cloud account are needed. The lab runs Oxy in its mock mode: the gateway runs every check on a request and then answers it itself, so nothing you send leaves your machine.

## Modules

| # | Module | Time |
|---|---|---|
| 1 | [From `git clone` to the first key](01-first-key.md) | 15 min |
| 2 | [The dashboard: spend, keys and refusals](02-dashboard.md) | 10 min |
| 3 | [The rules, before they bite](03-rules.md) | 10 min |
| 4 | [Budgets](04-budgets.md) | 10 min |
| 5 | [DLP](05-dlp.md) | 10 min |
| 6 | [The kill switch](06-kill-switch.md) | 10 min |
| 7 | [The audit row](07-audit-row.md) | 10 min |

Do the modules in order: each one starts from the state the one before it left. Module 1 includes the first build, which takes a few minutes; the rest of the time is yours.

## Before you start

You need:

- Docker Desktop, or Docker Engine with Compose v2. The stack uses about 1.5 GB of memory and about 12 GB of disk (images, build cache and volumes).
- `git`, `curl` and `jq` in a terminal (bash or zsh), and a browser.
- Ports 3000, 8000, 8025 and 8080 free on your machine.

Check the ports before the build:

```bash
lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(3000|8000|8025|8080) '
```

No output means the ports are free. If something is listed, stop it for the length of the lab, or see "Common gotchas" in [`docs/getting-started/quickstart.md`](../getting-started/quickstart.md).

Use one terminal for the whole lab, opened in the repository folder. The modules keep your key and, later, a signed-in session in shell variables, with two small functions that hand them to curl, and a new terminal starts without any of them.

## How the modules read

Every command block is meant to be copied as it is. The block after it, marked **Expected output**, is what the command printed when this lab was tested. Times, ids and key characters differ on your machine; the shape and the status codes do not.

## Tested on

Oxy [v1.0.6](https://github.com/heyvaldemar/oxy-llm-gateway/releases/tag/v1.0.6), on 2026-11-23: a clean Docker host (Docker Engine 29.9.3, Compose 5.7.1), every command in zsh 5.9, the browser steps in Chromium 140. Module 1 checks out that tag, so the commands keep matching their output when `main` moves on.

## When you are done

Module 7 ends with the cleanup. If you stop earlier, remove everything the lab created from the repository folder:

```bash
docker compose down -v
```

This deletes the containers and the volumes, with the users, keys and audit rows in them.

## Maintainer

Maintained by [DevOpsPink](https://github.com/DevOpsPink), co-author of Oxy, who is responsible for its developer experience (see [MAINTAINERS.md](../../MAINTAINERS.md)). Issues and pull requests about the lab are welcome.
