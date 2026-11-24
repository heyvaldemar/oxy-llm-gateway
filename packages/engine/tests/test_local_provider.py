import asyncio
import json
import httpx
import pytest

from providers.local_provider import OllamaProvider, VLLMProvider, OpenAICompatibleProvider, LOCAL_MODEL_TIMEOUT
from providers.local_provider import get_local_provider, LOCAL_HTTP_BASE_URL


class MockResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}
        self.request = None

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=self.request, response=self)


class MockClient:
    def __init__(self, responder):
        self.responder = responder

    async def post(self, url, json=None, timeout=None, headers=None):
        return self.responder("post", url, json, timeout)

    async def get(self, url, timeout=None):
        return self.responder("get", url, None, timeout)


@pytest.mark.asyncio
async def test_ollama_complete_normalizes_model_and_calls_openai_endpoint():
    captured = {}

    def responder(method, url, payload, timeout):
        captured["method"] = method
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout"] = timeout
        return MockResponse(
            200,
            {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3}},
        )

    client = MockClient(responder)
    provider = OllamaProvider(api_key="", http_client=client, base_url="http://ollama.local")
    result = await provider.complete("ollama/llama3", [{"role": "user", "content": "hi"}])

    assert captured["url"] == "http://ollama.local/v1/chat/completions"
    assert captured["payload"]["model"] == "llama3"
    assert result["choices"][0]["message"]["content"] == "ok"
    assert captured["timeout"] == LOCAL_MODEL_TIMEOUT


@pytest.mark.asyncio
async def test_vllm_complete_uses_openai_compat():
    captured = {}

    def responder(method, url, payload, timeout):
        captured["url"] = url
        return MockResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    client = MockClient(responder)
    provider = VLLMProvider(api_key="", http_client=client, base_url="http://vllm.local")
    result = await provider.complete("vllm/mistral-7b", [{"role": "user", "content": "hi"}])
    assert "v1/chat/completions" in captured["url"]
    assert result["choices"][0]["message"]["content"] == "ok"


@pytest.mark.asyncio
async def test_custom_http_handles_non_200():
    def responder(method, url, payload, timeout):
        return MockResponse(500, {"error": "boom"})

    client = MockClient(responder)
    provider = OpenAICompatibleProvider(api_key="", http_client=client, base_url="http://local-http")
    with pytest.raises(httpx.HTTPStatusError):
        await provider.complete("local/phi", [{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_local_provider_timeout_maps_to_http_error(monkeypatch):
    def responder(method, url, payload, timeout):
        raise httpx.TimeoutException("timeout")

    client = MockClient(responder)
    provider = VLLMProvider(api_key="", http_client=client, base_url="http://vllm.local")
    with pytest.raises(httpx.HTTPStatusError):
        await provider.complete("vllm/mistral-7b", [{"role": "user", "content": "hi"}])


def test_local_http_provider_factory(monkeypatch):
    monkeypatch.setenv("LOCAL_HTTP_BASE_URL", "http://custom.local")
    from importlib import reload
    import providers.local_provider as lp
    reload(lp)

    provider = lp.get_local_provider("local/phi3", api_key="", http_client=MockClient(lambda *args, **kwargs: None))
    assert isinstance(provider, lp.OpenAICompatibleProvider)
