"""Local models (Ollama, vLLM, Docker Model Runner, a generic endpoint) through the request path.

Up to 1.0.6 a request for ollama/..., vllm/... or local/... answered 400
"Provider not configured" for want of a provider key the model never needed, so
the adapters in providers/ could not be reached. They needed one more thing the
tests below pin: a provider instance holds the API key it was built with, and the
registry built one per provider name, so every organization's request left with
the first organization's key.
"""
import json
import random
import string
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core import resources
from providers import PricingEngine, provider_registry
from providers import local_provider
from providers.local_provider import NO_KEY_PLACEHOLDER


class Res:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


class Http:
    """`providers` lookups answer with `rows`; everything else is the local server."""

    def __init__(self, rows=None, status=200):
        self.rows, self.status, self.upstream = rows or [], status, []

    async def get(self, url, params=None, headers=None, timeout=None):
        assert url.endswith("/rest/v1/providers"), url
        return Res(self.status, self.rows)

    async def post(self, url, json=None, headers=None, timeout=None):
        self.upstream.append({"url": url, "json": json, "headers": headers or {}})
        return httpx.Response(200, json={
            "id": "c1", "object": "chat.completion", "model": json["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1500, "completion_tokens": 2000, "total_tokens": 3500},
        }, request=httpx.Request("POST", url))


@pytest.fixture
def key_lookup(monkeypatch):
    for name in ("OPENAI_API_KEY", "OLLAMA_API_KEY", "VLLM_API_KEY", "DOCKER_API_KEY", "LOCAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    http = Http()
    monkeypatch.setattr(resources, "http_client", http)
    return http


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "vllm", "docker", "local"])
async def test_a_local_provider_needs_no_key(key_lookup, provider):
    from routes.proxy import get_provider_api_key
    assert await get_provider_api_key("org-1", provider) == NO_KEY_PLACEHOLDER


@pytest.mark.asyncio
async def test_a_local_server_started_with_a_key_gets_it_from_the_environment(key_lookup, monkeypatch):
    from routes.proxy import get_provider_api_key
    monkeypatch.setenv("VLLM_API_KEY", "the-servers-own-key")
    assert await get_provider_api_key("org-1", "vllm") == "the-servers-own-key"


@pytest.mark.asyncio
async def test_a_hosted_provider_without_a_key_is_still_refused(key_lookup):
    from routes.proxy import get_provider_api_key
    with pytest.raises(HTTPException) as exc:
        await get_provider_api_key("org-1", "openai")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_a_failed_lookup_is_not_read_as_no_key(key_lookup):
    from routes.proxy import get_provider_api_key
    key_lookup.status = 500
    with pytest.raises(HTTPException) as exc:
        await get_provider_api_key("org-1", "vllm")
    assert exc.value.status_code == 500


def test_each_organizations_key_reaches_its_own_provider_instance():
    client = httpx.AsyncClient()
    a = provider_registry.get_provider_for_model("vllm/m", "key-of-organization-A", client)
    b = provider_registry.get_provider_for_model("vllm/m", "key-of-organization-B", client)
    again = provider_registry.get_provider_for_model("vllm/m", "key-of-organization-A", client)
    assert (a.api_key, b.api_key, again.api_key) == ("key-of-organization-A", "key-of-organization-B", "key-of-organization-A")


def test_the_registry_keeps_no_key_after_the_call():
    from providers.base import ProviderRegistry
    registry = ProviderRegistry()
    registry.register("vllm", local_provider.VLLMProvider, model_prefixes=["vllm/"])
    registry.get_provider_for_model("vllm/m", "key-that-must-not-be-kept", httpx.AsyncClient())
    held = repr(vars(registry)) + repr([vars(x) for x in registry._provider_instances.values()])
    assert "key-that-must-not-be-kept" not in held


def test_a_local_model_bills_nothing_unless_the_operator_prices_it(monkeypatch):
    monkeypatch.delenv("LOCAL_PROMPT_PRICE_PER_MILLION", raising=False)
    monkeypatch.delenv("LOCAL_COMPLETION_PRICE_PER_MILLION", raising=False)
    assert PricingEngine.calculate_cost("vllm/qwen2.5", 1500, 2000) == 0
    monkeypatch.setenv("LOCAL_PROMPT_PRICE_PER_MILLION", "1.00")
    monkeypatch.setenv("LOCAL_COMPLETION_PRICE_PER_MILLION", "2.00")
    assert PricingEngine.calculate_cost("vllm/qwen2.5", 1500, 2000) == Decimal("0.0055")
    monkeypatch.setenv("LOCAL_PROMPT_PRICE_PER_MILLION", "not a number")
    assert PricingEngine.calculate_cost("ollama/llama3.2", 1500, 0) == 0
    # An unknown hosted model keeps the conservative default.
    assert PricingEngine.calculate_cost("some-new-hosted-model", 1500, 2000) > 0


def test_local_slash_uses_the_configured_endpoint(monkeypatch):
    monkeypatch.setenv("LOCAL_HTTP_BASE_URL", "http://gpu-box:9000")
    provider = provider_registry.get_provider_for_model("local/m", NO_KEY_PLACEHOLDER, httpx.AsyncClient())
    assert provider.base_url == "http://gpu-box:9000"


# ----------------------------------------------------------------------------- the request path
KEY = {
    "organization_id": "org-1", "project_id": "proj-1", "user_id": "user-1", "key_id": "key-1",
    "budget_limit": 0, "api_key": "oxy_live_test", "plan_tier": "free", "org_paused": False,
    "privacy_mode": "metadata_only", "dlp_mode": "block",
}


class Redis:
    async def incr(self, name):
        return 1

    def __getattr__(self, name):
        async def nothing(*args, **kwargs):
            return None
        return nothing


@pytest.fixture
def gateway(monkeypatch, key_lookup):
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
    monkeypatch.setattr(resources, "supabase", None)  # the organization's status then reads as active
    monkeypatch.setattr(local_provider, "VLLM_BASE_URL", "http://vllm.test:8000")
    app = FastAPI()
    app.include_router(p.router)
    return SimpleNamespace(client=TestClient(app, raise_server_exceptions=False), rows=rows, http=key_lookup)


def _chat(client, content="hello", model="vllm/qwen2.5"):
    return client.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": content}]},
                       headers={"Authorization": "Bearer oxy_live_test"})


def test_a_request_for_a_local_model_is_served_with_no_provider_key(gateway):
    r = _chat(gateway.client)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "hello"
    (call,) = gateway.http.upstream
    assert call["url"] == "http://vllm.test:8000/v1/chat/completions"
    assert call["json"]["model"] == "qwen2.5", "the vllm/ prefix is the gateway's, not the server's"
    assert "Authorization" not in call["headers"], "a server given no key is sent none"
    assert [(row["provider"], row["status"]) for row in gateway.rows] == [("vllm", "success")]


def test_a_secret_bound_for_a_local_model_never_leaves(gateway):
    token = "ghp_" + "".join(random.choice(string.ascii_letters + string.digits) for _ in range(36))
    r = _chat(gateway.client, content="my token is " + token)
    assert r.status_code == 451
    assert gateway.http.upstream == []
    assert [row["threat_type"] for row in gateway.rows] == ["github_token"]


def test_a_local_server_with_its_own_key_is_sent_it(gateway, monkeypatch):
    monkeypatch.setenv("VLLM_API_KEY", "the-servers-own-key")
    r = _chat(gateway.client)
    assert r.status_code == 200, r.text
    assert gateway.http.upstream[0]["headers"] == {"Authorization": "Bearer the-servers-own-key"}
