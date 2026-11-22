#!/usr/bin/env python3
"""Markdown summary of proofs/results.json and proofs/plants.json (for the CI job summary)."""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name):
    p = HERE / name
    return json.loads(p.read_text()) if p.exists() else {}


def rows(r, plants):
    """(proof, claim, measured, holds, planted violation caught: True/False/None) per proof in results.json."""
    rows = []
    if "budget" in r:
        b = r["budget"]
        rows.append(("budget", "Budget", "%s requests, %s at a time, budget $%s: provider served %s (the budget buys %s), overspend $%s; "
                     "after the worker's sync cycles %s of %s served, after Redis lost the counter %s of %s"
                     % (b.get("requests"), b.get("concurrent"), b.get("budget_usd"), b.get("served_by_provider"),
                        b.get("max_affordable"), b.get("overspend_usd"),
                        (b.get("after_sync_cycles") or {}).get("served"), (b.get("after_sync_cycles") or {}).get("requests"),
                        (b.get("after_counter_lost") or {}).get("served"), (b.get("after_counter_lost") or {}).get("requests")), b["ok"]))
    if "dlp" in r:
        d = r["dlp"]
        rows.append(("dlp", "DLP", "%s secret types sent, %s reached the provider" % (d.get("secret_types"), d.get("reached_provider")), d["ok"]))
    if "kill" in r:
        k = r["kill"]
        rows.append(("kill", "Kill switch", "%s requests after the kill, %s reached the provider; first blocked response %s ms after the call"
                     % (k.get("requests_after_kill"), k.get("reached_provider_after_kill"), k.get("first_blocked_response_ms")), k["ok"]))
    if "local" in r:
        l = r["local"]
        rows.append(("local", "Local model", "%s requests for %s, budget $%s: the server served %s (the budget buys %s), overspend $%s; %s secret types sent, %s reached the server; %s requests after the kill, %s reached it"
                     % (l.get("requests"), l.get("model"), l.get("budget_usd"), l.get("served_by_server"), l.get("max_affordable"),
                        l.get("overspend_usd"), (l.get("dlp") or {}).get("secret_types"), (l.get("dlp") or {}).get("reached_server"),
                        len((l.get("kill") or {}).get("statuses_after") or []), (l.get("kill") or {}).get("reached_server_after_kill")), l["ok"]))
    if "tenancy" in r:
        t = r["tenancy"]
        rows.append(("tenancy", "Tenancy", "%s requests with another tenant's agent and session ids: %s refused, %s reached the provider"
                     % (t.get("foreign_requests"), t.get("refused_403"), t.get("reached_provider")), t["ok"]))
    if "revoke" in r:
        v = r["revoke"]
        rows.append(("revoke", "Revocation", "%s requests with a cached key after revoking it: %s refused, %s reached the provider"
                     % (v.get("requests_after_revoke"), v.get("refused_401"), v.get("reached_provider_after_revoke")), v["ok"]))
    if "failclosed" in r:
        f = r["failclosed"]
        rows.append(("failclosed", "Fail closed", "Redis paused: %s budgeted and %s killed-session requests, %s and %s reached the provider"
                     % ((f.get("budgeted") or {}).get("requests"), (f.get("killed_session") or {}).get("requests"),
                        (f.get("budgeted") or {}).get("reached_provider"), (f.get("killed_session") or {}).get("reached_provider")), f["ok"]))
    if "surface" in r:
        s = r["surface"]
        rows.append(("surface", "Surface", "%s database functions probed with the browser's keys, %s callable; %s tables, %s without RLS; %s policies open to the browser roles"
                     % (s.get("functions_probed"), len(s.get("callable_by_browser_roles") or []), s.get("tables"),
                        len(s.get("tables_without_rls") or []), len(s.get("policies_open_to_browser_roles") or [])), s["ok"]))
    if "keys" in r:
        k = r["keys"]
        rows.append(("keys", "Keys at rest", "%s tenant keys checked against every name and value of %s Redis keys: %s found"
                     % (k.get("tenant_keys_checked"), k.get("redis_keys_scanned"), len(k.get("tenants_whose_key_is_in_redis") or [])), k["ok"]))
    if "audit" in r:
        a = r["audit"]
        lim, rev = a.get("rate_limited") or {}, a.get("revoke_keeps_rows") or {}
        rows.append(("audit", "Audit", "%s requests sent (%s refused by the burst limit), %s request_logs rows; "
                     "a dashboard revoke kept %s of the key's %s rows"
                     % (a.get("requests_sent"), lim.get("requests"), a.get("request_log_rows"),
                        rev.get("rows_after_revoke"), rev.get("rows_before_revoke")), a["ok"]))

    out = []
    for key, name, text, ok in rows:
        mine = [v for v in plants.values() if v.get("proof") == key]
        out.append((key, name, text, ok, None if not mine else all(v.get("caught") for v in mine)))
    return out


def main():
    print("### Proofs\n")
    print("| Claim | Measured | Holds | Planted violation caught |")
    print("|---|---|---|---|")
    for key, name, text, ok, caught in rows(load("results.json"), load("plants.json")):
        print("| %s | %s | %s | %s |" % (name, text, "yes" if ok else "**no**",
                                          "yes" if caught else ("**no**" if caught is False else "not run")))


if __name__ == "__main__":
    main()
