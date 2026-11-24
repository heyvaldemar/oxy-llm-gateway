#!/usr/bin/env python3
"""
Each proof, shown the violation it exists for.

For every proof in run_proofs.py this plants the bug it is meant to catch in
the engine's source (bind-mounted, so a restart picks it up) or in the live
database, runs that proof and requires it to FAIL, then restores everything.
A proof that still holds with its violation planted proves nothing, and this
script exits non-zero.

  budget        reservation becomes read, wait, write (not atomic)
  budget_ledger the old ledger: the budget worker moves the spend counter out of
                Redis each cycle and a missing counter reads as zero
  dlp           the DLP scan reports nothing
  kill          the gateway ignores a session's kill state
  kill_record   a killed session's row loses who, when and why
  local_key     a local model needs a provider key again (400, never served)
  local_price   a local model's price is ignored: no budget ever caps it
  tenancy       agent and session headers are not checked against the key's organization
  revoke        revoking a key leaves its cached record in Redis
  failclosed    with Redis down, the budget check and the kill check fail open
  surface       browser roles may execute every function; webhooks loses RLS;
                a policy lets anyone insert into sso_audit_log
  keys          the Redis cache is named by the raw API key
  audit         request_logs rows are never written
  audit_revoke  the dashboard's revoke deletes the key and request_logs.api_key_id
                cascades: the key's audit rows go with it (1.0.2 and before)
  audit_429     a request refused by the burst limit gets no audit row

Run against a stack started with proofs/docker-compose.proofs.yml. The compose
command is taken from OXY_COMPOSE (default: the two files from the repo root).

  python3 proofs/plant_violations.py [--only budget,dlp,...]
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "packages" / "engine"
COMPOSE = shlex.split(os.getenv("OXY_COMPOSE", "docker compose -f docker-compose.yml -f proofs/docker-compose.proofs.yml"))

RESERVE_CALL = (
    "                result = await self._reserve_script(\n"
    "                    keys=[budget_key, credits_key],\n"
    "                    args=[str(amount), str(budget_limit), organization_id]\n"
    "                )\n"
)
NON_ATOMIC_RESERVE = '''                # PLANTED VIOLATION (proofs/plant_violations.py): read, wait, write.
                raw = await self.redis.get(budget_key)
                current = Decimal(str(raw or "0"))
                import asyncio as _asyncio
                await _asyncio.sleep(0.05)
                if current + amount > budget_limit:
                    result = ["BUDGET_EXCEEDED", str(current), str(current), str(budget_limit), "0"]
                else:
                    await self.redis.set(budget_key, str(current + amount))
                    result = ["OK", str(current + amount), str(current), str(budget_limit), "0"]
'''
SEED_CHECK = (
    "if redis.call('EXISTS', budget_key) == 0 then\n"
    "    return {'SEED_REQUIRED', '0', '0', tostring(budget_limit), '0'}\n"
    "end\n"
)

# name: proof that must fail, proofs to run, source edits (file, old, new), SQL (plant, undo), services to restart
PLANTS = {
    "budget": {
        "proof": "budget", "run": "budget",
        "edits": [("core/budget.py", RESERVE_CALL, NON_ATOMIC_RESERVE)],
    },
    "budget_ledger": {
        "proof": "budget", "run": "budget",
        "edits": [("workers/budget_worker.py",
                   "                        spend = await redis_client.get(key)\n",
                   "                        spend = await redis_client.getdel(key)  # PLANTED VIOLATION\n"),
                  ("core/budget.py", SEED_CHECK, "-- PLANTED VIOLATION: a missing counter reads as zero\n")],
        "restart": ["engine", "worker-budget"],
    },
    "dlp": {
        "proof": "dlp", "run": "dlp",
        "edits": [("routes/proxy.py",
                   "    dlp_result = scan_for_secrets(messages)\n",
                   "    dlp_result = {\"threat_detected\": False, \"threat_type\": \"none\", \"threat_count\": 0, \"details\": []}  # PLANTED VIOLATION\n")],
    },
    "kill": {
        "proof": "kill", "run": "kill",
        "edits": [("routes/proxy.py",
                   "        terminated = bool(row.get(\"terminated\"))\n        if not terminated:\n",
                   "        terminated = False  # PLANTED VIOLATION: the kill state is ignored\n        if False:\n")],
    },
    "kill_record": {
        "proof": "kill", "run": "kill",
        "sql": ("CREATE FUNCTION planted_forget_kill() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN NEW.kill_requested_at := NULL; NEW.kill_requested_by := NULL; NEW.kill_reason := NULL; RETURN NEW; END $$; "
                "CREATE TRIGGER planted_forget_kill BEFORE UPDATE ON agent_sessions "
                "FOR EACH ROW EXECUTE FUNCTION planted_forget_kill();",
                "DROP TRIGGER IF EXISTS planted_forget_kill ON agent_sessions; DROP FUNCTION IF EXISTS planted_forget_kill();"),
        "edits": [],
    },
    "local_key": {
        "proof": "local", "run": "local",
        "edits": [("routes/proxy.py",
                   "        if provider_name in LOCAL_PROVIDERS and exc.status_code == 400:\n",
                   "        if False:  # PLANTED VIOLATION: a local model needs a provider key again\n")],
    },
    "local_price": {
        "proof": "local", "run": "local",
        "edits": [("providers/pricing.py",
                   "            \"prompt_tokens_per_million\": price(\"LOCAL_PROMPT_PRICE_PER_MILLION\"),\n"
                   "            \"completion_tokens_per_million\": price(\"LOCAL_COMPLETION_PRICE_PER_MILLION\"),\n",
                   "            \"prompt_tokens_per_million\": Decimal(\"0\"),  # PLANTED VIOLATION: the operator's price is ignored\n"
                   "            \"completion_tokens_per_million\": Decimal(\"0\"),\n")],
    },
    "tenancy": {
        "proof": "tenancy", "run": "tenancy",
        "edits": [("routes/proxy.py",
                   "        if row is None or row.get(\"organization_id\") != str(organization_id):\n",
                   "        if row is None:  # PLANTED VIOLATION: any organization's session\n"),
                  ("routes/proxy.py",
                   "        if owner != str(organization_id):\n",
                   "        if owner is None:  # PLANTED VIOLATION: any organization's agent\n")],
    },
    "revoke": {
        "proof": "revoke", "run": "revoke",
        "edits": [("routes/api_keys_v1.py",
                   "            await forget_cached_api_key(row.get(\"key_hash\"), row.get(\"project_id\"))\n",
                   "            pass  # PLANTED VIOLATION: the cached record stays\n")],
    },
    "failclosed": {
        "proof": "failclosed", "run": "failclosed",
        "edits": [("routes/proxy.py",
                   "            except Exception as e:\n                log.error(\n                    \"budget_check_failed\",\n",
                   "            except Exception as e:\n                pass  # PLANTED VIOLATION: fail open\n            if False:\n                log.error(\n                    \"budget_check_failed\",\n"),
                  ("routes/proxy.py",
                   "            # Redis cannot answer: the database decides below. Never \"assume alive\".\n"
                   "            log_error_safe(log, \"session_context_cache_failed\", e, session_id=session_id)\n",
                   "            # PLANTED VIOLATION: Redis down, the session is assumed alive.\n"
                   "            row = await _session_row_from_db(session_id)\n"
                   "            return ({**row, \"terminated\": False} if row else None), True\n")],
    },
    "surface": {
        "proof": "surface", "run": "surface",
        "sql": ("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO anon, authenticated; "
                "ALTER TABLE webhooks DISABLE ROW LEVEL SECURITY; "
                "CREATE POLICY planted_open_insert ON sso_audit_log FOR INSERT WITH CHECK (true);",
                "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated; "
                "ALTER TABLE webhooks ENABLE ROW LEVEL SECURITY; "
                "DROP POLICY IF EXISTS planted_open_insert ON sso_audit_log;"),
        "edits": [],
    },
    "keys": {
        "proof": "keys", "run": "dlp,keys",
        "edits": [("dependencies.py",
                   "    return api_key_cache_name_for_hash(hash_key(api_key))\n",
                   "    return f\"api_key:{api_key}\"  # PLANTED VIOLATION\n")],
    },
    "audit": {
        "proof": "audit", "run": "dlp,audit",
        "edits": [("core/costs.py",
                   "    if supabase_available:\n",
                   "    if False:  # PLANTED VIOLATION\n")],
    },
    "audit_revoke": {
        "proof": "audit", "run": "audit",
        "sql": ("ALTER TABLE request_logs DROP CONSTRAINT request_logs_api_key_id_fkey, "
                "ADD CONSTRAINT request_logs_api_key_id_fkey FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE;",
                "ALTER TABLE request_logs DROP CONSTRAINT request_logs_api_key_id_fkey, "
                "ADD CONSTRAINT request_logs_api_key_id_fkey FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE SET NULL;"),
        "edits": [("main.py",
                   "        res = await core.resources.http_client.patch(\n"
                   "            f\"{SUPABASE_URL}/rest/v1/api_keys\",\n"
                   "            params={\"id\": f\"eq.{key_id}\", \"project_id\": f\"eq.{project_id}\"},\n"
                   "            headers=get_supabase_headers(prefer=\"return=representation\"),\n"
                   "            json={\"is_active\": False, \"deactivated_reason\": \"revoked\",\n"
                   "                  \"deactivated_at\": datetime.now(timezone.utc).isoformat()},\n"
                   "        )\n",
                   "        res = await core.resources.http_client.delete(  # PLANTED VIOLATION: the key is deleted\n"
                   "            f\"{SUPABASE_URL}/rest/v1/api_keys\",\n"
                   "            params={\"id\": f\"eq.{key_id}\", \"project_id\": f\"eq.{project_id}\"},\n"
                   "            headers=get_supabase_headers(prefer=\"return=representation\"),\n"
                   "        )\n")],
    },
    "audit_429": {
        "proof": "audit", "run": "audit",
        "edits": [("routes/proxy.py",
                   "        if exc.status_code == 429:\n            await record_blocked_request(\n",
                   "        if False:  # PLANTED VIOLATION: no audit row for a 429\n            await record_blocked_request(\n")],
    },
}


def compose(*args):
    subprocess.run(COMPOSE + list(args), cwd=ROOT, check=True, stdout=subprocess.DEVNULL)


def restart(services=("engine",)):
    compose("restart", *services)
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://localhost:8000/health", timeout=3) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(2)
    sys.exit("engine did not come back after restart")


def psql(sql):
    compose("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "oxy", "-v", "ON_ERROR_STOP=1", "-c", sql)


def run_proof(names, out):
    r = subprocess.run([sys.executable, str(ROOT / "proofs" / "run_proofs.py"), "--only", names, "--out", str(out)], cwd=ROOT,
                       capture_output=True, text=True)
    return r.returncode, json.loads(out.read_text()) if out.exists() else {}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=",".join(PLANTS))
    ap.add_argument("--out", default=str(ROOT / "proofs" / "plants.json"))
    args = ap.parse_args()
    report, failed_to_fail = {}, []
    for name in [s.strip() for s in args.only.split(",") if s.strip()]:
        plant = PLANTS[name]
        services = plant.get("restart", ["engine"])
        originals = {}
        try:
            for rel, old, new in plant["edits"]:
                path = ENGINE / rel
                originals.setdefault(path, path.read_text())
                current = path.read_text()
                if current.count(old) != 1:
                    sys.exit("plant %s: anchor not found exactly once in %s" % (name, rel))
                path.write_text(current.replace(old, new))
            if plant.get("sql"):
                psql(plant["sql"][0])
            if plant["edits"]:
                restart(services)
            out = ROOT / "proofs" / ("plant-%s.json" % name)
            code, results = run_proof(plant["run"], out)
            proof = plant["proof"]
            caught = code != 0 and not results.get(proof, {}).get("ok", True)
            report[name] = {"proof": proof, "caught": caught, "proof_result": results.get(proof)}
            print("%-13s planted -> proof %-10s %s" % (name, proof, "FAILED, as it must" if caught else "STILL HOLDS: the proof cannot see this bug"))
            if not caught:
                failed_to_fail.append(name)
        finally:
            if plant.get("sql"):
                psql(plant["sql"][1])
            for path, text in originals.items():
                path.write_text(text)
            if originals:
                restart(services)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    sys.exit(1 if failed_to_fail else 0)


if __name__ == "__main__":
    main()
