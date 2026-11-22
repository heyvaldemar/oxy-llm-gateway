#!/usr/bin/env python3
"""
Proofs: what Oxy claims, measured against a recording fake provider.

  budget    N concurrent requests against a small project budget. Money spent at
            the provider (completions served x price) never exceeds the budget,
            and stays there after the budget worker's sync cycles and after Redis
            loses the spend counter.
  dlp       Requests carrying secrets of every type the gateway detects. None of
            them reaches the provider.
  kill      An agent session keeps sending requests. Another tenant's attempt to
            kill it is refused; then it is killed through the documented endpoint.
            No request started after the kill returns reaches the provider; the
            time to the first blocked response is measured. The session's row
            records the kill: who, when and why.
  local     A local model (vllm/...) gets the same gateway as a hosted one: served by
            the fake with no provider key anywhere, capped by a budget at the
            operator's price per million tokens, a secret in it never reaches the
            server, and a killed session's next request does not either.
  tenancy   Another tenant sends requests carrying this tenant's agent and session
            ids. All are refused; none reaches the provider or lands on this
            tenant's agent.
  revoke    A key that was just used (and so is cached) is revoked; its next
            requests are refused and none reaches the provider.
  failclosed Redis stops answering. Requests whose budget or kill switch cannot be
            checked through Redis are refused or decided by the database, never
            served unchecked: 0 of them reach the provider.
  surface   With the browser's anon key, and with a signed-in user's token, no
            database function of the engine can be called through /rest/v1/rpc;
            every table has row-level security and no policy lets the browser
            roles read or write everything.
  keys      No tenant's API key is in Redis, in any key name or value.
  audit     Every request the proofs sent has a row in request_logs, blocked
            ones included.

Start the stack with proofs/docker-compose.proofs.yml first. The compose command
(for redis-cli, psql and pausing Redis) comes from OXY_COMPOSE, default: the two
files from the repo root. Each proof signs up its own users, so budgets do not
interfere. Secrets are generated at random on every run; none is stored in the
repository.

  python3 proofs/run_proofs.py [--only budget,dlp,...] [--out proofs/results.json]

Exit status 0 only if every selected proof holds.
"""

import argparse
import json
import secrets
import os
import random
import re
import shlex
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://localhost:8000"
SUPA = "http://localhost:8080"
FAKE = "http://127.0.0.1:9000"
MODEL = "gpt-4o-mini"

LOCAL_MODEL = "vllm/proof-model"  # served by the same fake, with no provider key anywhere
LOCAL_BUDGET_USD = Decimal("0.02")
LOCAL_REQUESTS = 12
# One fake completion (1500 prompt + 2000 completion tokens) at the proofs overlay's
# LOCAL_PROMPT_PRICE_PER_MILLION=1.00 and LOCAL_COMPLETION_PRICE_PER_MILLION=2.00.
LOCAL_UNIT_USD = Decimal("0.0055")

BUDGET_USD = Decimal("0.05")
BUDGET_REQUESTS = 500
BUDGET_CONCURRENCY = 100
BUDGET_LATE_REQUESTS = 20  # after the sync cycles, and again after the counter is lost
SYNC_WAIT_SECONDS = 7      # the proofs overlay runs the budget worker every 2 s

COMPOSE = shlex.split(os.getenv("OXY_COMPOSE", "docker compose -f docker-compose.yml -f proofs/docker-compose.proofs.yml"))


def compose(*args, check=True):
    return subprocess.run(COMPOSE + list(args), cwd=ROOT, check=check, capture_output=True, text=True)


def redis_cli(*args):
    """redis-cli inside the Redis container; the password stays in its environment."""
    r = compose("exec", "-T", "redis", "sh", "-c", 'REDISCLI_AUTH="$REDIS_PASSWORD" exec redis-cli --raw "$@"', "--", *args)
    return r.stdout


def psql(sql):
    r = compose("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "oxy", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql)
    return [line for line in r.stdout.splitlines() if line.strip()]


# ----------------------------------------------------------------------------- http
def http(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status, hdrs = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, dict(e.headers)
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, None, {}  # no answer within the timeout
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = raw.decode("utf-8", "replace")
    return status, payload, hdrs


def compose_default(name):
    """The stack's value: the environment, an env file compose reads (COMPOSE_ENV_FILES, as in
    the devcontainer), else the local default in docker-compose.yml as ${NAME:-value}."""
    if os.environ.get(name):
        return os.environ[name]
    for f in [x for x in os.environ.get("COMPOSE_ENV_FILES", "").split(",") if x]:
        for line in Path(f).read_text().splitlines() if Path(f).exists() else []:
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    text = (ROOT / "docker-compose.yml").read_text()
    m = re.search(r"\$\{%s:-([^}]+)\}" % re.escape(name), text)
    if not m:
        sys.exit("cannot find the %s default in docker-compose.yml" % name)
    return m.group(1)


ANON = compose_default("NEXT_PUBLIC_SUPABASE_ANON_KEY")
SERVICE = compose_default("SUPABASE_SERVICE_KEY")
SERVICE_HEADERS = {"apikey": SERVICE, "Authorization": "Bearer " + SERVICE}


# ----------------------------------------------------------------------------- fixtures
class Tenant:
    """A fresh user with an onboarded org, project and Oxy API key."""

    def __init__(self, label, budget):
        self.label = label
        run = uuid.uuid4().hex[:10]
        email = "proof-%s-%s@example.com" % (label, run)
        st, body, _ = http("POST", SUPA + "/auth/v1/signup", {"email": email, "password": "proof-" + run + "-local"},
                           {"apikey": ANON})
        assert st == 200 and body.get("access_token"), "sign-up failed: %s" % st
        self.jwt = body["access_token"]
        csrf = "proof" + run
        st, body, _ = http("POST", API + "/management/onboard", {"full_name": "Proof " + label, "email": email},
                           {"Authorization": "Bearer " + self.jwt, "X-CSRF-Token": csrf, "Cookie": "csrf_token=" + csrf})
        assert st == 200 and str(body.get("api_key", "")).startswith("oxy_"), "onboarding failed: %s" % st
        self.key, self.org, self.project = body["api_key"], body["organization_id"], body["project_id"]
        # Budget before the first request, so the key cache never holds another value.
        st, _, _ = http("PATCH", "%s/rest/v1/projects?id=eq.%s" % (SUPA, self.project), {"budget_limit": str(budget)},
                        {**SERVICE_HEADERS, "Prefer": "return=minimal"})
        assert st in (200, 204), "setting the budget failed: %s" % st
        self.sent = 0
        self._lock = threading.Lock()

    def chat(self, content, extra_headers=None, key=None, counted=True, model=None):
        """One completion request. counted: it belongs to this tenant's audit trail."""
        if counted:
            with self._lock:
                self.sent += 1
        st, body, _ = http("POST", API + "/v1/chat/completions",
                           {"model": model or MODEL, "messages": [{"role": "user", "content": content}]},
                           {"Authorization": "Bearer " + (key or self.key), **(extra_headers or {})})
        return st, body

    def jwt_headers(self):
        csrf = "proofcsrf" + uuid.uuid4().hex[:8]
        return {"Authorization": "Bearer " + self.jwt, "X-CSRF-Token": csrf, "Cookie": "csrf_token=" + csrf}

    def agent_session(self):
        """A registered agent and a running session for it: (agent_id, session_id)."""
        auth = self.jwt_headers()
        st, agent, _ = http("POST", API + "/v1/agents",
                            {"name": "proof-agent-" + uuid.uuid4().hex[:6], "project_id": self.project, "default_model": MODEL}, auth)
        assert st in (200, 201), "creating the agent failed: %s %s" % (st, str(agent)[:200])
        agent_id = (agent.get("agent") or agent).get("id") if isinstance(agent, dict) else None
        st, sess, _ = http("POST", "%s/v1/agents/%s/sessions" % (API, agent_id), {}, auth)
        assert st in (200, 201), "starting the session failed: %s %s" % (st, str(sess)[:200])
        s = sess.get("session") or sess
        return agent_id, s.get("id") or s.get("session_id")


def fake_reset():
    http("POST", FAKE + "/_reset")


def fake_requests():
    return http("GET", FAKE + "/_requests")[1]


def unit_cost():
    """Price of one fake completion (1500 prompt + 2000 completion tokens) from the engine's own table."""
    text = (ROOT / "packages/engine/providers/pricing.py").read_text()
    block = text[text.index('"%s"' % MODEL):]
    p = Decimal(re.search(r'"prompt_tokens_per_million":\s*Decimal\("([0-9.]+)"\)', block).group(1))
    c = Decimal(re.search(r'"completion_tokens_per_million":\s*Decimal\("([0-9.]+)"\)', block).group(1))
    return (p * 1500 + c * 2000) / Decimal(1_000_000)


def tagged(bodies, tag):
    out = []
    for r in bodies:
        text = json.dumps(r.get("body"))
        if tag in text:
            out.append((r, text))
    return out


# ----------------------------------------------------------------------------- proofs
def proof_budget(results, tenants):
    t = Tenant("budget", BUDGET_USD)
    tenants.append(t)
    fake_reset()
    # A key in use is already cached; warm it so the burst measures the budget, not a cold cache.
    statuses = [t.chat("[proof:budget:warmup] ping")[0]]
    barrier = threading.Barrier(BUDGET_CONCURRENCY)

    def one(i):
        if i < BUDGET_CONCURRENCY:
            barrier.wait()  # the first wave leaves together
        return t.chat("[proof:budget:%d] ping" % i)[0]

    with ThreadPoolExecutor(max_workers=BUDGET_CONCURRENCY) as pool:
        statuses += list(pool.map(one, range(BUDGET_REQUESTS)))
    served_burst = len(tagged(fake_requests(), "[proof:budget:"))

    # The budget worker copies the spend counter to Postgres every cycle. It used to
    # move it (GETDEL), which handed the project its whole budget again each minute.
    time.sleep(SYNC_WAIT_SECONDS)
    after_sync = [t.chat("[proof:budget:aftersync:%d] ping" % i)[0] for i in range(BUDGET_LATE_REQUESTS)]
    mirrored = psql("SELECT current_spend FROM projects WHERE id = '%s'" % t.project)
    # Redis loses the counter (flush, eviction, restart without its data): the next
    # reservation must resume from the Postgres mirror, not from zero.
    redis_cli("DEL", "project_spend:" + t.project)
    after_loss = [t.chat("[proof:budget:afterloss:%d] ping" % i)[0] for i in range(BUDGET_LATE_REQUESTS)]
    statuses += after_sync + after_loss

    served = len(tagged(fake_requests(), "[proof:budget:"))
    unit = unit_cost()
    spent = unit * served
    overspend = max(Decimal(0), spent - BUDGET_USD)
    counts = {str(s): statuses.count(s) for s in sorted(set(statuses))}
    ok = (overspend == 0 and served_burst >= 1 and counts.get("402", 0) >= 1
          and served == served_burst and all(s == 402 for s in after_sync + after_loss))
    results["budget"] = {
        "ok": ok, "requests": len(statuses), "concurrent": BUDGET_CONCURRENCY, "budget_usd": str(BUDGET_USD),
        "unit_cost_usd": str(unit), "served_by_provider": served, "spent_usd": str(spent),
        "overspend_usd": str(overspend), "max_affordable": int(BUDGET_USD / unit), "statuses": counts,
        "served_in_burst": served_burst,
        "after_sync_cycles": {"requests": len(after_sync), "served": sum(1 for s in after_sync if s == 200)},
        "after_counter_lost": {"requests": len(after_loss), "served": sum(1 for s in after_loss if s == 200)},
        "postgres_mirror_usd": mirrored[0] if mirrored else None,
    }


def _rand(n, alphabet=string.ascii_letters + string.digits):
    return "".join(random.choice(alphabet) for _ in range(n))


def _luhn_card(prefix="4"):
    digits = [int(d) for d in prefix] + [random.randint(0, 9) for _ in range(15 - len(prefix))]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return "".join(map(str, digits)) + str((10 - total % 10) % 10)


def throwaway_private_key():
    """A real Ed25519 private key, generated for this run and thrown away."""
    return subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519"], check=True,
                          capture_output=True, text=True).stdout.strip()


def synthetic_secrets():
    """One random value per secret type the gateway detects (core.py DLP_PATTERNS)."""
    up = string.ascii_uppercase + string.digits
    b64 = string.ascii_letters + string.digits + "_-"
    return {
        "openai_key": "sk-" + _rand(48),
        "openai_project_key": "sk-proj-" + _rand(48),
        "anthropic_key": "sk-ant-api03-" + _rand(95, b64),
        "aws_key": "AKIA" + _rand(16, up),
        "stripe_live_key": "sk_live_" + _rand(28),
        "jwt": "eyJ" + _rand(20, b64) + "." + _rand(30, b64) + "." + _rand(30, b64),
        "private_key": throwaway_private_key(),
        "credit_card": _luhn_card("4"),
        "github_token": "ghp_" + _rand(36),
        "slack_token": "xoxb-" + "-".join(_rand(12, string.digits) for _ in range(2)) + "-" + _rand(24),
        "google_api_key": "AIza" + _rand(35, b64),
    }


def proof_dlp(results, tenants):
    t = Tenant("dlp", Decimal("0"))
    tenants.append(t)
    fake_reset()
    secrets = synthetic_secrets()
    per_type = {}
    for name, value in secrets.items():
        st, _ = t.chat("[proof:dlp:%s] please keep this safe: %s" % (name, value))
        per_type[name] = {"status": st}
    # A clean control request must still go through.
    control_status, _ = t.chat("[proof:dlp:control] nothing secret here")
    bodies = json.dumps(fake_requests())
    reached = [name for name, value in secrets.items() if value in bodies or value.replace("\n", "\\n") in bodies]
    for name in secrets:
        per_type[name]["reached_provider"] = name in reached
    ok = not reached and control_status == 200
    results["dlp"] = {"ok": ok, "secret_types": len(secrets), "reached_provider": len(reached),
                      "reached_types": reached, "control_request_status": control_status, "per_type": per_type}


def proof_kill(results, tenants):
    t = Tenant("kill", Decimal("0"))
    tenants.append(t)
    fake_reset()
    agent_id, session_id = t.agent_session()
    headers = {"X-Oxy-Agent-Id": agent_id, "X-Oxy-Session-Id": session_id}
    # Another tenant must not be able to kill this session.
    intruder = Tenant("intruder", Decimal("0"))
    foreign_status, _, _ = http("POST", "%s/v1/agent-sessions/%s/kill" % (API, session_id), None,
                                {"Authorization": "Bearer " + intruder.key})

    log, stop = [], threading.Event()
    log_lock = threading.Lock()

    def worker():
        while not stop.is_set():
            started = time.time()
            st, _ = t.chat("[proof:kill:%.6f] step" % started, headers)
            with log_lock:
                log.append((started, time.time(), st))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    time.sleep(2.0)
    kill_sent = time.time()
    kst, kbody, _ = http("POST", "%s/v1/agent-sessions/%s/kill" % (API, session_id), None, {"Authorization": "Bearer " + t.key})
    kill_done = time.time()
    time.sleep(3.0)
    stop.set()
    for th in threads:
        th.join()

    served = tagged(fake_requests(), "[proof:kill:")
    started_at = [float(re.search(r"\[proof:kill:([0-9.]+)\]", text).group(1)) for _, text in served]
    before = sum(1 for x in started_at if x < kill_sent)
    after = sum(1 for x in started_at if x > kill_done)
    blocked = sorted(end for start, end, st in log if start >= kill_sent and st in (409, 410))
    first_block_ms = round((blocked[0] - kill_sent) * 1000) if blocked else None
    late_ok = sum(1 for start, _, st in log if start > kill_done and st == 200)
    # The session's row says who killed it, when and why: that is what an operator reads afterwards.
    row = psql("SELECT status, kill_requested_at IS NOT NULL, kill_requested_by IS NOT NULL, coalesce(kill_reason, ''), "
               "ended_at IS NOT NULL FROM agent_sessions WHERE id = '%s'" % session_id)
    row_records_the_kill = row == ["killed|t|t|manual_kill|t"]
    ok = (kst == 200 and foreign_status == 404 and before >= 1 and after == 0 and late_ok == 0
          and first_block_ms is not None and row_records_the_kill)
    results["kill"] = {
        "ok": ok, "kill_status": kst, "foreign_tenant_kill_status": foreign_status,
        "kill_call_ms": round((kill_done - kill_sent) * 1000),
        "first_blocked_response_ms": first_block_ms, "reached_provider_before_kill": before,
        "reached_provider_after_kill": after, "requests_after_kill": sum(1 for s0, _, _ in log if s0 > kill_done),
        "answered_200_after_kill": late_ok, "row_records_the_kill": row_records_the_kill,
        "statuses": {str(k): sum(1 for _, _, s1 in log if s1 == k) for k in sorted({s1 for _, _, s1 in log})},
    }


def proof_local(results, tenants):
    """A local model gets the same gateway as a hosted one. Requests for vllm/... reach the fake
    with no provider key configured (the prefix is the gateway's, not the server's); a budget
    caps them at the operator's price per million tokens; a secret in one never reaches the
    server; a killed session's next request does not either."""
    t = Tenant("local", LOCAL_BUDGET_USD)
    tenants.append(t)
    fake_reset()
    statuses = [t.chat("[proof:local:budget:%d] ping" % i, model=LOCAL_MODEL)[0] for i in range(LOCAL_REQUESTS)]
    served_rows = tagged(fake_requests(), "[proof:local:budget:")
    served = len(served_rows)
    models_sent = sorted({r["body"].get("model") for r, _ in served_rows})
    spent = LOCAL_UNIT_USD * served
    overspend = max(Decimal(0), spent - LOCAL_BUDGET_USD)
    counts = {str(c): statuses.count(c) for c in sorted(set(statuses))}

    d = Tenant("local_dlp", Decimal("0"))
    tenants.append(d)
    secrets = synthetic_secrets()
    dlp_statuses = {name: d.chat("[proof:local:dlp:%s] keep this safe: %s" % (name, value), model=LOCAL_MODEL)[0]
                    for name, value in secrets.items()}
    control_status, _ = d.chat("[proof:local:dlp:control] nothing secret here", model=LOCAL_MODEL)
    bodies = json.dumps(fake_requests())
    reached = [name for name, value in secrets.items() if value in bodies or value.replace("\n", "\\n") in bodies]

    k = Tenant("local_kill", Decimal("0"))
    tenants.append(k)
    agent_id, session_id = k.agent_session()
    headers = {"X-Oxy-Agent-Id": agent_id, "X-Oxy-Session-Id": session_id}
    before_status, _ = k.chat("[proof:local:kill:before] step", headers, model=LOCAL_MODEL)
    kill_status, _, _ = http("POST", "%s/v1/agent-sessions/%s/kill" % (API, session_id), None, {"Authorization": "Bearer " + k.key})
    after = [k.chat("[proof:local:kill:after:%d] step" % i, headers, model=LOCAL_MODEL)[0] for i in range(5)]
    reached_after_kill = len(tagged(fake_requests(), "[proof:local:kill:after:"))

    ok = (served >= 1 and served == statuses.count(200) and counts.get("402", 0) >= 1 and overspend == 0
          and models_sent == ["proof-model"]
          and not reached and all(st == 451 for st in dlp_statuses.values()) and control_status == 200
          and before_status == 200 and kill_status == 200 and all(st == 410 for st in after) and reached_after_kill == 0)
    results["local"] = {
        "ok": ok, "model": LOCAL_MODEL, "model_sent_to_server": models_sent,
        "budget_usd": str(LOCAL_BUDGET_USD), "unit_cost_usd": str(LOCAL_UNIT_USD), "requests": len(statuses),
        "served_by_server": served, "max_affordable": int(LOCAL_BUDGET_USD / LOCAL_UNIT_USD), "spent_usd": str(spent),
        "overspend_usd": str(overspend), "statuses": counts,
        "dlp": {"secret_types": len(secrets), "refused_451": sum(1 for st in dlp_statuses.values() if st == 451),
                "reached_server": len(reached), "control_request_status": control_status},
        "kill": {"before_status": before_status, "kill_status": kill_status, "statuses_after": after,
                 "reached_server_after_kill": reached_after_kill},
    }


def proof_tenancy(results, tenants):
    """Another tenant's requests carrying this tenant's agent and session ids are refused."""
    owner = Tenant("tenancy-owner", Decimal("0"))
    other = Tenant("tenancy-other", Decimal("0"))
    tenants += [owner, other]
    fake_reset()
    agent_id, session_id = owner.agent_session()
    attempts = ([{"X-Oxy-Agent-Id": agent_id, "X-Oxy-Session-Id": session_id}] * 10
                + [{"X-Oxy-Session-Id": session_id}] * 10 + [{"X-Oxy-Agent-Id": agent_id}] * 5)
    statuses = [other.chat("[proof:tenancy:foreign:%d] step" % i, h)[0] for i, h in enumerate(attempts)]
    control, _ = owner.chat("[proof:tenancy:own] step", {"X-Oxy-Agent-Id": agent_id, "X-Oxy-Session-Id": session_id})
    reached = len(tagged(fake_requests(), "[proof:tenancy:foreign:"))
    on_owner_agent = psql("SELECT count(*) FROM request_logs WHERE organization_id = '%s' AND (agent_id = '%s' OR session_id = '%s')"
                          % (other.org, agent_id, session_id))
    on_owner_agent = int(on_owner_agent[0]) if on_owner_agent else None
    ok = all(st == 403 for st in statuses) and reached == 0 and control == 200 and on_owner_agent == 0
    results["tenancy"] = {
        "ok": ok, "foreign_requests": len(statuses), "refused_403": sum(1 for st in statuses if st == 403),
        "reached_provider": reached, "logged_on_the_owners_agent": on_owner_agent, "owner_request_status": control,
        "statuses": {str(k): statuses.count(k) for k in sorted(set(statuses))},
    }


def proof_revoke(results, tenants):
    """A cached key, revoked, is refused on its very next request."""
    t = Tenant("revoke", Decimal("0"))
    tenants.append(t)
    fake_reset()
    st, made, _ = http("POST", API + "/v1/api-keys", {"name": "proof-revoke"}, {"Authorization": "Bearer " + t.key})
    assert st in (200, 201) and str((made or {}).get("secret", "")).startswith("oxy_"), "creating a key failed: %s" % st
    second_key, key_id = made["secret"], made["id"]
    before, _ = t.chat("[proof:revoke:before] ping", key=second_key)  # caches the key
    rst, _, _ = http("DELETE", "%s/v1/api-keys/%s" % (API, key_id), None, {"Authorization": "Bearer " + t.key})
    after = [t.chat("[proof:revoke:after:%d] ping" % i, key=second_key, counted=False)[0] for i in range(10)]
    reached_after = len(tagged(fake_requests(), "[proof:revoke:after:"))
    ok = before == 200 and rst == 200 and all(s == 401 for s in after) and reached_after == 0
    results["revoke"] = {
        "ok": ok, "status_before_revoke": before, "revoke_status": rst, "requests_after_revoke": len(after),
        "refused_401": sum(1 for s in after if s == 401), "reached_provider_after_revoke": reached_after,
    }


def proof_failclosed(results, tenants):
    """Redis stops answering: what it guards is refused or decided by the database, never served unchecked."""
    budgeted = Tenant("failclosed-budget", BUDGET_USD)
    killer = Tenant("failclosed-kill", Decimal("0"))
    control = Tenant("failclosed-control", Decimal("0"))
    tenants += [budgeted, killer, control]
    agent_id, session_id = killer.agent_session()
    for t in (budgeted, killer, control):
        assert t.chat("[proof:failclosed:warmup] ping")[0] == 200, "warm-up request failed"
    kst, _, _ = http("POST", "%s/v1/agent-sessions/%s/kill" % (API, session_id), None, {"Authorization": "Bearer " + killer.key})
    assert kst == 200, "kill failed: %s" % kst
    fake_reset()
    session_headers = {"X-Oxy-Agent-Id": agent_id, "X-Oxy-Session-Id": session_id}
    jobs = ([("budget", budgeted, None)] * 4 + [("kill", killer, session_headers)] * 4 + [("control", control, None)] * 2)
    compose("pause", "redis")
    paused_at = time.time()
    try:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            out = list(pool.map(lambda j: (j[0], j[1].chat("[proof:failclosed:%s] ping" % j[0], j[2])[0]), jobs))
    finally:
        compose("unpause", "redis")
    paused_for = round(time.time() - paused_at, 1)
    for _ in range(30):
        if redis_cli("PING").strip() == "PONG":
            break
        time.sleep(1)
    served = fake_requests()
    by = {k: [st for kind, st in out if kind == k] for k in ("budget", "kill", "control")}
    reached = {k: len(tagged(served, "[proof:failclosed:%s]" % k)) for k in by}
    ok = (reached["budget"] == 0 and reached["kill"] == 0
          and all(st in (402, 503) for st in by["budget"]) and all(st in (409, 410, 503) for st in by["kill"]))
    results["failclosed"] = {
        "ok": ok, "redis_paused_seconds": paused_for,
        "budgeted": {"requests": len(by["budget"]), "statuses": by["budget"], "reached_provider": reached["budget"]},
        "killed_session": {"requests": len(by["kill"]), "statuses": by["kill"], "reached_provider": reached["kill"]},
        "unguarded_control": {"requests": len(by["control"]), "statuses": by["control"], "reached_provider": reached["control"]},
    }


def _schema_functions():
    """Every function in db/schema.sql with its argument names and types."""
    text = (ROOT / "db" / "schema.sql").read_text()
    out = {}
    for name, args in re.findall(r"CREATE OR REPLACE FUNCTION\s+(?:public\.)?(\w+)\s*\((.*?)\)\s*RETURNS", text, re.S):
        params = []
        for part in [a.strip() for a in args.split(",") if a.strip()]:
            bits = part.split()
            if len(bits) >= 2 and bits[0].upper() not in ("IN", "OUT", "INOUT"):
                params.append((bits[0], bits[1].upper()))
        out[name] = params
    return out


def _dummy(sqltype):
    if sqltype.startswith("UUID"):
        return str(uuid.uuid4())
    if sqltype.startswith(("INT", "BIGINT", "SMALLINT", "NUMERIC", "DECIMAL", "REAL", "DOUBLE")):
        return 0
    if sqltype.startswith("BOOL"):
        return False
    if sqltype.startswith("JSON"):
        return {}
    return "proof"


def proof_surface(results, tenants):
    """The browser roles cannot call the engine's database functions or read unprotected tables."""
    t = Tenant("surface", Decimal("0"))
    tenants.append(t)
    roles = {"anon": {"apikey": ANON}, "authenticated": {"apikey": ANON, "Authorization": "Bearer " + t.jwt}}
    reachable = []
    funcs = _schema_functions()
    for name, params in funcs.items():
        if name.startswith(("trg_", "trigger_", "update_updated_at", "handle_")):
            continue  # trigger functions cannot be called through /rpc
        body = {p: _dummy(ty) for p, ty in params}
        for role, hdrs in roles.items():
            st, payload, _ = http("POST", "%s/rest/v1/rpc/%s" % (SUPA, name), body, hdrs)
            denied = st in (401, 403) or (isinstance(payload, dict) and payload.get("code") == "42501")
            if not denied and st != 404:
                reachable.append("%s as %s (HTTP %s)" % (name, role, st))
    leaked = []
    for table in ("admin_audit_logs", "notification_settings", "org_monthly_usage", "webhooks"):
        for role, hdrs in roles.items():
            st, payload, _ = http("GET", "%s/rest/v1/%s?select=*&limit=1" % (SUPA, table), None, hdrs)
            if st == 200 and payload:
                leaked.append("%s readable as %s" % (table, role))
        st, _, _ = http("POST", "%s/rest/v1/webhooks" % SUPA, {"organization_id": t.org, "url": "https://example.com/hook"},
                        {**roles["anon"], "Prefer": "return=minimal"})
        if st in (200, 201, 204):
            leaked.append("webhooks writable as anon")
            break
    # Row-level security on every table, and no policy that opens a whole table to
    # the browser roles (PUBLIC, anon, authenticated). Three catalogs are public by
    # design and readable only.
    tables_total = psql("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind = 'r'")
    without_rls = psql("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                       "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity ORDER BY 1")
    open_policies = psql("SELECT tablename || ' ' || policyname || ' ' || cmd FROM pg_policies "
                         "WHERE schemaname = 'public' AND roles && ARRAY['public', 'anon', 'authenticated']::name[] "
                         "AND (qual = 'true' OR with_check = 'true') "
                         "AND NOT (cmd = 'SELECT' AND tablename IN ('achievement_types', 'prompts', 'prompt_ratings')) ORDER BY 1")
    results["surface"] = {"ok": not reachable and not leaked and not without_rls and not open_policies and bool(tables_total),
                          "functions_probed": len(funcs), "callable_by_browser_roles": reachable,
                          "table_exposure": sorted(set(leaked)), "tables": int(tables_total[0]) if tables_total else None,
                          "tables_without_rls": without_rls, "policies_open_to_browser_roles": open_policies}


REDIS_DUMP = """
local out = {}
for _, k in ipairs(redis.call('KEYS', '*')) do
  table.insert(out, k)
  local t = redis.call('TYPE', k).ok
  if t == 'string' then table.insert(out, redis.call('GET', k))
  elseif t == 'set' then for _, m in ipairs(redis.call('SMEMBERS', k)) do table.insert(out, m) end
  elseif t == 'hash' then for _, m in ipairs(redis.call('HGETALL', k)) do table.insert(out, m) end
  elseif t == 'list' then for _, m in ipairs(redis.call('LRANGE', k, 0, -1)) do table.insert(out, m) end
  elseif t == 'zset' then for _, m in ipairs(redis.call('ZRANGE', k, 0, -1)) do table.insert(out, m) end
  end
end
return out
"""


def proof_keys(results, tenants):
    """No tenant API key is in Redis: not in a key name, not in a value."""
    names = [n for n in redis_cli("--scan").splitlines() if n.strip()]
    everything = redis_cli("EVAL", REDIS_DUMP, "0")
    keys = [t.key for t in tenants]
    found = sorted({t.label for t in tenants if t.key in everything})
    cached = sum(1 for n in names if n.startswith("api_key:"))
    ok = bool(keys) and cached >= 1 and not found
    results["keys"] = {"ok": ok, "redis_keys_scanned": len(names), "cached_key_records": cached,
                       "tenant_keys_checked": len(keys), "tenants_whose_key_is_in_redis": found}


def log_rows(query):
    """request_logs rows matching a PostgREST filter, counted by the database."""
    st, _, hdrs = http("GET", "%s/rest/v1/request_logs?%s&select=id" % (SUPA, query), None,
                       {**SERVICE_HEADERS, "Prefer": "count=exact", "Range": "0-0"})
    total = (hdrs.get("Content-Range") or hdrs.get("content-range") or "*/0").split("/")[-1]
    return int(total) if total.isdigit() else 0


def audit_revoke(tenants):
    """Revoking a key in the dashboard keeps every audit row the key made.
    Up to 1.0.2 the dashboard deleted the key and request_logs.api_key_id
    cascaded: the key's rows, refusals included, went with it."""
    t = Tenant("auditrevoke", Decimal("0"))
    tenants.append(t)
    auth = t.jwt_headers()
    st, made, _ = http("POST", "%s/management/key?projectId=%s&name=proof-audit-revoke" % (API, t.project), None, auth)
    assert st == 200 and str((made or {}).get("api_key", "")).startswith("oxy_"), "creating a key failed: %s" % st
    key = made["api_key"]
    st, listed, _ = http("GET", "%s/management/keys?projectId=%s" % (API, t.project), None, t.jwt_headers())
    key_id = next((k["id"] for k in (listed or []) if k.get("name") == "proof-audit-revoke"), None)
    assert st == 200 and key_id, "the new key is not in the dashboard's list: %s" % st
    served = [t.chat("[proof:auditrevoke:%d] ping" % i, key=key)[0] for i in range(3)]
    secret = "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
    refused, _ = t.chat("[proof:auditrevoke:dlp] " + secret, key=key)
    made_rows = len(served) + 1
    before = 0
    for _ in range(30):  # served rows are written in the background
        before = log_rows("api_key_id=eq.%s" % key_id)
        if before >= made_rows:
            break
        time.sleep(1)
    rst, _, _ = http("DELETE", "%s/management/key/%s?projectId=%s" % (API, key_id, t.project), None, t.jwt_headers())
    next_status, _ = t.chat("[proof:auditrevoke:after] ping", key=key, counted=False)  # 401: no project, no row
    after = log_rows("api_key_id=eq.%s" % key_id)
    rule = (psql("SELECT confdeltype FROM pg_constraint WHERE conname = 'request_logs_api_key_id_fkey'") or ["?"])[0]
    ok = (all(s == 200 for s in served) and refused == 451 and rst == 200 and next_status == 401
          and before == made_rows and after == before and rule != "c")
    return {"ok": ok, "rows_before_revoke": before, "revoke_status": rst, "next_request_status": next_status,
            "rows_after_revoke": after, "on_delete": {"n": "SET NULL", "c": "CASCADE", "r": "RESTRICT", "a": "NO ACTION"}.get(rule, rule)}


def audit_rate_limited(tenants):
    """A request refused by the burst limit (429) has its audit row too. The proofs
    raise the limit, so the user's burst counter is set past it in Redis: the real
    limiter refuses, with nothing else changed."""
    t = Tenant("auditlimit", Decimal("0"))
    tenants.append(t)
    st, user, _ = http("GET", SUPA + "/auth/v1/user", None, {"apikey": ANON, "Authorization": "Bearer " + t.jwt})
    assert st == 200 and user.get("id"), "reading the user failed: %s" % st
    redis_cli("SET", "burst:" + user["id"], "1000000000", "EX", "30")
    statuses = [t.chat("[proof:auditlimit:%d] ping" % i)[0] for i in range(5)]
    redis_cli("DEL", "burst:" + user["id"])
    return {"ok": all(s == 429 for s in statuses), "requests": len(statuses), "statuses": statuses}


def proof_audit(results, tenants):
    """Every request sent by the proofs above has a request_logs row, refusals
    included: by the budget, DLP, the kill switch, another tenant's ids, the
    burst limit. Revoking a key keeps its rows."""
    def step(fn):
        try:
            return fn(tenants)
        except AssertionError as e:
            return {"ok": False, "error": str(e)}
    revoke = step(audit_revoke)
    limited = step(audit_rate_limited)
    expected = sum(t.sent for t in tenants)
    got = None
    for _ in range(30):
        got = 0
        for t in tenants:
            st, _, hdrs = http("GET", "%s/rest/v1/request_logs?organization_id=eq.%s&select=id" % (SUPA, t.org), None,
                               {**SERVICE_HEADERS, "Prefer": "count=exact", "Range": "0-0"})
            rng = hdrs.get("Content-Range") or hdrs.get("content-range") or "*/0"
            got += int(rng.split("/")[-1]) if rng.split("/")[-1].isdigit() else 0
        if got >= expected:
            break
        time.sleep(1)
    results["audit"] = {"ok": got == expected and revoke["ok"] and limited["ok"], "requests_sent": expected,
                        "request_log_rows": got, "rate_limited": limited, "revoke_keeps_rows": revoke,
                        "per_proof": {t.label: t.sent for t in tenants}}


PROOFS = {"budget": proof_budget, "dlp": proof_dlp, "kill": proof_kill, "local": proof_local, "tenancy": proof_tenancy,
          "revoke": proof_revoke, "failclosed": proof_failclosed, "surface": proof_surface, "keys": proof_keys}
ORDER = ["budget", "dlp", "kill", "local", "tenancy", "revoke", "failclosed", "surface", "keys", "audit"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=",".join(ORDER))
    ap.add_argument("--out", default=str(ROOT / "proofs" / "results.json"))
    args = ap.parse_args()
    wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    selected = [n for n in ORDER if n in wanted]
    results, tenants = {}, []
    for name in selected:
        if name == "audit":
            continue
        try:
            PROOFS[name](results, tenants)
        except AssertionError as e:
            results[name] = {"ok": False, "error": str(e)}
    if "audit" in selected:
        proof_audit(results, tenants)
    Path(args.out).write_text(json.dumps(results, indent=2) + "\n")
    for name, r in results.items():
        print("%-6s %s  %s" % (name, "HOLDS" if r["ok"] else "FAILS", json.dumps({k: v for k, v in r.items() if k not in ("ok", "per_type")})))
    sys.exit(0 if results and all(r["ok"] for r in results.values()) else 1)


if __name__ == "__main__":
    main()
