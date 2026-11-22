"""A secret that sits only in a tool definition is named, not "(none)".

Up to 1.0.5 the request path took the type of the first scan that found
anything with `or`, and the message scan hands back the string "none" when it
finds nothing, which is truthy: a secret found only in `tools` (or in the legacy
`functions`) was still refused with 451, but the answer said "Sensitive data
detected (none)", and the audit row and the log carried the type "none".
"""
import random
import string
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import resources

KEY = {
    "organization_id": "org-1", "project_id": "proj-1", "user_id": "user-1", "key_id": "key-1",
    "budget_limit": 0, "api_key": "oxy_live_test", "plan_tier": "free", "org_paused": False,
    "privacy_mode": "metadata_only", "dlp_mode": "block",
}


def _token():
    # Built at run time: no token-shaped literal lives in the repository.
    return "ghp_" + "".join(random.choice(string.ascii_letters + string.digits) for _ in range(36))


class Redis:
    async def incr(self, name):
        return 1

    def __getattr__(self, name):
        async def nothing(*args, **kwargs):
            return None
        return nothing


@pytest.fixture
def proxy(monkeypatch):
    from routes import proxy as p

    rows = []

    async def caller():
        return dict(KEY)

    async def record(**kwargs):
        rows.append(kwargs)

    async def org_status(org_id):
        return {"status": "active"}

    monkeypatch.setattr(p, "get_user_and_key_from_api_key", lambda: caller())
    monkeypatch.setattr(p, "get_org_status", org_status)
    monkeypatch.setattr(p, "record_request", record)
    monkeypatch.setattr(resources, "redis", Redis())
    app = FastAPI()
    app.include_router(p.router)
    client = TestClient(app, raise_server_exceptions=False)
    return SimpleNamespace(client=client, rows=rows)


def _post(client, **extra):
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}], **extra}
    return client.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer oxy_live_test"})


def _tool(secret):
    return {"type": "function", "function": {"name": "lookup", "description": f"Use this token: {secret}", "parameters": {"type": "object"}}}


def test_a_secret_only_in_tools_is_named(proxy):
    r = _post(proxy.client, tools=[_tool(_token())])
    assert r.status_code == 451
    error = r.json()["error"]
    assert error["code"] == "DLP_BLOCKED"
    assert "(tool_github_token)" in error["message"] and "(none)" not in error["message"]
    assert "none" not in error["hint"]
    assert [row["threat_type"] for row in proxy.rows] == ["tool_github_token"]


def test_a_secret_only_in_legacy_functions_is_named(proxy):
    r = _post(proxy.client, functions=[_tool(_token())["function"]])
    assert r.status_code == 451
    assert "(tool_github_token)" in r.json()["error"]["message"]
    assert [row["threat_type"] for row in proxy.rows] == ["tool_github_token"]


def test_a_secret_in_a_message_keeps_its_own_type(proxy):
    r = _post(proxy.client, messages=[{"role": "user", "content": _token()}], tools=[_tool(_token())])
    assert r.status_code == 451
    assert "(github_token)" in r.json()["error"]["message"]
    assert [row["threat_type"] for row in proxy.rows] == ["github_token"]
