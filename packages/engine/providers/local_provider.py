"""
Local Model Provider - Ollama/vLLM/Docker Model Runner
=======================================================

Enables routing to locally-hosted models for:
- Cost savings (no API costs)
- Data privacy (no external calls)
- Latency optimization (on-premise)
- Offline operation
- Air-gapped environments

Supported Backends:
- Ollama (http://localhost:11434)
- vLLM (http://localhost:8000)
- Docker Model Runner
- Any OpenAI-compatible local server

Model Naming Convention:
- "ollama/llama3.2" → Ollama backend
- "vllm/mistral-7b" → vLLM backend
- "local/phi-3" → Auto-detect backend
- "llama3.2" → Direct Ollama model name

Security:
- All requests still go through DLP scanning
- Budget tracking works (cost = $0 for local)
- Audit logging enabled
- Rate limiting applies

"""

import json
import time
import os
from typing import Dict, Any, Optional, List, AsyncGenerator
from decimal import Decimal

import httpx
import structlog

from .base import LLMProvider

# Routes/proxy.py hands a local model this placeholder when no key is configured.
NO_KEY_PLACEHOLDER = "no-key-local-model"


def _auth_headers(api_key: Optional[str]) -> Dict[str, str]:
    """No Authorization header for a local server that was given no key; a bearer one for a
    server started with its own (vLLM's --api-key)."""
    if not api_key or api_key == NO_KEY_PLACEHOLDER:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


log = structlog.get_logger()


# ============================================================================
# CONFIGURATION
# ============================================================================

# Environment-configurable endpoints
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://localhost:8000")
LOCAL_HTTP_BASE_URL = os.getenv("LOCAL_HTTP_BASE_URL")  # generic OpenAI-compatible
LOCAL_MODEL_TIMEOUT = int(os.getenv("LOCAL_MODEL_TIMEOUT", "120"))  # Local models can be slow

# Model prefixes for backend detection
OLLAMA_PREFIXES = ["ollama/", "llama", "mistral", "codellama", "phi", "gemma", "qwen", "deepseek"]
VLLM_PREFIXES = ["vllm/"]
LOCAL_PREFIXES = ["local/"]


# ============================================================================
# OLLAMA PROVIDER
# ============================================================================

class OllamaProvider(LLMProvider):
    """
    Ollama local model provider.
    
    Ollama API is OpenAI-compatible at /v1/chat/completions.
    Also supports native /api/chat endpoint.
    
    Setup:
        1. Install Ollama: https://ollama.ai
        2. Pull a model: ollama pull llama3.2
        3. Start server: ollama serve
        4. Use in Oxy: model="ollama/llama3.2" or model="llama3.2"
    
    Features:
        - Streaming support
        - Tool/function calling (model-dependent)
        - Vision models (llava, etc.)
        - Embeddings (optional)
    """
    
    def __init__(
        self,
        api_key: str,  # Not used for Ollama, but kept for interface consistency
        http_client: httpx.AsyncClient,
        provider_name: str = "ollama",
        base_url: Optional[str] = None
    ):
        super().__init__(api_key, http_client, provider_name)
        self.base_url = base_url or OLLAMA_BASE_URL
        self.log = log.bind(provider="ollama", base_url=self.base_url)
    
    def _normalize_model_name(self, model: str) -> str:
        """
        Normalize model name for Ollama.
        
        "ollama/llama3.2" → "llama3.2"
        "llama3.2" → "llama3.2"
        """
        if model.startswith("ollama/"):
            return model[7:]  # Remove "ollama/" prefix
        if model.startswith("local/"):
            return model[6:]  # Remove "local/" prefix
        return model
    
    async def _check_health(self) -> bool:
        """Check if Ollama server is running."""
        try:
            response = await self.http_client.get(
                f"{self.base_url}/api/tags",
                timeout=5.0
            )
            return response.status_code == 200
        except Exception:
            return False
    
    async def complete(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Non-streaming Ollama completion via OpenAI-compatible API.
        
        Falls back to native /api/chat if OpenAI endpoint fails.
        """
        normalized_model = self._normalize_model_name(model)
        
        # Use OpenAI-compatible endpoint (preferred)
        url = f"{self.base_url}/v1/chat/completions"
        
        payload = {
            "model": normalized_model,
            "messages": messages,
            "stream": False
        }
        
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        
        # Pass through tools if provided (Ollama supports this)
        if "tools" in kwargs:
            payload["tools"] = kwargs["tools"]
        if "tool_choice" in kwargs:
            payload["tool_choice"] = kwargs["tool_choice"]
        
        try:
            self.log.info("ollama_request", model=normalized_model, messages_count=len(messages))
            
            response = await self.http_client.post(
                url,
                json=payload,
                headers=_auth_headers(self.api_key),
                timeout=LOCAL_MODEL_TIMEOUT
            )
            
            if response.status_code == 404:
                # Model might not be pulled yet
                raise httpx.HTTPStatusError(
                    f"Model '{normalized_model}' not found. Run: ollama pull {normalized_model}",
                    request=response.request,
                    response=response
                )
            
            response.raise_for_status()
            result = response.json()
            
            self.log.info("ollama_response", 
                         model=normalized_model,
                         tokens=result.get("usage", {}).get("total_tokens", 0))
            
            return result

        except httpx.ConnectError:
            self.log.error("ollama_connection_failed", base_url=self.base_url)
            raise httpx.HTTPStatusError(
                f"Cannot connect to Ollama at {self.base_url}. Is Ollama running?",
                request=None,
                response=None
            )
        except httpx.TimeoutException as e:
            self.log.error("local_provider_timeout", provider="ollama", base_url=self.base_url)
            raise httpx.HTTPStatusError(
                "Local provider timeout",
                request=None,
                response=None
            )
        except httpx.HTTPStatusError as e:
            self.log.error("ollama_error", 
                          status=e.response.status_code if e.response else None,
                          error=str(e)[:200])
            raise
    
    async def stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        Streaming Ollama completion.
        
        Returns SSE-formatted chunks compatible with OpenAI format.
        """
        normalized_model = self._normalize_model_name(model)
        url = f"{self.base_url}/v1/chat/completions"
        
        payload = {
            "model": normalized_model,
            "messages": messages,
            "stream": True
        }
        
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if "tools" in kwargs:
            payload["tools"] = kwargs["tools"]
        
        try:
            async with self.http_client.stream(
                "POST", 
                url, 
                json=payload,
                headers=_auth_headers(self.api_key),
                timeout=LOCAL_MODEL_TIMEOUT
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        yield line + "\n"
                    elif line.strip():
                        yield f"data: {line}\n"
                        
        except httpx.ConnectError:
            self.log.error("ollama_stream_connection_failed", base_url=self.base_url)
            yield f'data: {{"error": "Cannot connect to Ollama at {self.base_url}"}}\n'
            yield "data: [DONE]\n"
    
    @staticmethod
    def supports_model(model_name: str) -> bool:
        """Check if model is a local Ollama model."""
        model_lower = model_name.lower()
        for prefix in OLLAMA_PREFIXES:
            if model_lower.startswith(prefix):
                return True
        return False
    
    def get_pricing(self, model: str) -> Dict[str, Decimal]:
        """Local models = zero cost."""
        return {
            "prompt_tokens_per_million": Decimal("0.00"),
            "completion_tokens_per_million": Decimal("0.00")
        }
    
    def calculate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int
    ) -> Decimal:
        """
        Local models have ZERO API cost.
        
        However, you can configure internal cost tracking if needed
        (e.g., for GPU time allocation, energy costs).
        """
        return Decimal("0.00")
    
    def get_supported_models(self) -> List[str]:
        """
        Return list of available Ollama models.
        
        In reality, this should query the Ollama server.
        For now, return common models.
        """
        return [
            "llama3.2", "llama3.1", "llama2",
            "mistral", "mistral-nemo",
            "codellama", "codellama:34b",
            "phi3", "phi3:medium",
            "gemma2", "gemma2:27b",
            "qwen2.5", "qwen2.5:72b",
            "deepseek-coder-v2",
        ]


# ============================================================================
# VLLM PROVIDER
# ============================================================================

class VLLMProvider(LLMProvider):
    """
    vLLM local model provider.
    
    vLLM is a high-throughput LLM serving engine.
    Uses OpenAI-compatible API by default.
    
    Setup:
        1. Install: pip install vllm
        2. Start: python -m vllm.entrypoints.openai.api_server --model mistralai/Mistral-7B-v0.1
        3. Use in Oxy: model="vllm/mistral-7b"
    
    Features:
        - High throughput (PagedAttention)
        - Tensor parallelism
        - Continuous batching
        - OpenAI-compatible API
    """
    
    def __init__(
        self,
        api_key: str,
        http_client: httpx.AsyncClient,
        provider_name: str = "vllm",
        base_url: Optional[str] = None
    ):
        super().__init__(api_key, http_client, provider_name)
        self.base_url = base_url or VLLM_BASE_URL
        self.log = log.bind(provider="vllm", base_url=self.base_url)
    
    def _normalize_model_name(self, model: str) -> str:
        """Remove vllm/ prefix if present."""
        if model.startswith("vllm/"):
            return model[5:]
        if model.startswith("local/"):
            return model[6:]
        return model
    
    async def complete(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        vLLM completion via OpenAI-compatible API.
        """
        normalized_model = self._normalize_model_name(model)
        url = f"{self.base_url}/v1/chat/completions"
        
        payload = {
            "model": normalized_model,
            "messages": messages,
            "stream": False
        }
        
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        
        try:
            self.log.info("vllm_request", model=normalized_model)
            
            response = await self.http_client.post(
                url,
                json=payload,
                headers=_auth_headers(self.api_key),
                timeout=LOCAL_MODEL_TIMEOUT
            )
            response.raise_for_status()
            
            result = response.json()
            self.log.info("vllm_response", 
                         model=normalized_model,
                         tokens=result.get("usage", {}).get("total_tokens", 0))
            
            return result
            
        except httpx.ConnectError:
            self.log.error("vllm_connection_failed", base_url=self.base_url)
            raise httpx.HTTPStatusError(
                f"Cannot connect to vLLM at {self.base_url}. Is vLLM server running?",
                request=None,
                response=None
            )
        except httpx.TimeoutException as e:
            self.log.error("local_provider_timeout", provider="vllm", base_url=self.base_url)
            raise httpx.HTTPStatusError(
                "Local provider timeout",
                request=None,
                response=None
            )
        except httpx.HTTPStatusError as e:
            self.log.error("vllm_error", status=e.response.status_code if e.response else None)
            raise
    
    async def stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """Streaming vLLM completion."""
        normalized_model = self._normalize_model_name(model)
        url = f"{self.base_url}/v1/chat/completions"
        
        payload = {
            "model": normalized_model,
            "messages": messages,
            "stream": True
        }
        
        try:
            async with self.http_client.stream(
                "POST", 
                url, 
                json=payload,
                headers=_auth_headers(self.api_key),
                timeout=LOCAL_MODEL_TIMEOUT
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        yield line + "\n"
        except httpx.TimeoutException as e:
            self.log.error("local_provider_timeout", provider="vllm", base_url=self.base_url, stream=True)
            raise httpx.HTTPStatusError(
                "Local provider timeout",
                request=None,
                response=None
            )
    
    @staticmethod
    def supports_model(model_name: str) -> bool:
        """Check if model is a vLLM model."""
        model_lower = model_name.lower()
        for prefix in VLLM_PREFIXES:
            if model_lower.startswith(prefix):
                return True
        return False
    
    def get_pricing(self, model: str) -> Dict[str, Decimal]:
        """Local models = zero cost."""
        return {
            "prompt_tokens_per_million": Decimal("0.00"),
            "completion_tokens_per_million": Decimal("0.00")
        }
    
    def calculate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int
    ) -> Decimal:
        """Local models = zero cost."""
        return Decimal("0.00")
    
    def get_supported_models(self) -> List[str]:
        """Return list of common vLLM models."""
        return [
            "mistralai/Mistral-7B-v0.1",
            "meta-llama/Llama-2-7b-hf",
            "meta-llama/Llama-3.1-8B",
            "Qwen/Qwen2-7B",
        ]


# ============================================================================
# DOCKER MODEL RUNNER PROVIDER
# ============================================================================

class DockerModelRunnerProvider(LLMProvider):
    """
    Docker Model Runner provider.
    
    Docker's native AI model runner (docker model run).
    Uses OpenAI-compatible API.
    
    Setup:
        1. Enable in Docker Desktop settings
        2. Run: docker model run llama3.2
        3. Use in Oxy: model="docker/llama3.2"
    
    Features:
        - Integrated with Docker Desktop
        - Automatic model management
        - GPU passthrough
    """
    
    DOCKER_MODEL_URL = os.getenv("DOCKER_MODEL_URL", "http://localhost:12434")
    
    def __init__(
        self,
        api_key: str,
        http_client: httpx.AsyncClient,
        provider_name: str = "docker",
        base_url: Optional[str] = None
    ):
        super().__init__(api_key, http_client, provider_name)
        self.base_url = base_url or self.DOCKER_MODEL_URL
        self.log = log.bind(provider="docker_model_runner")
    
    def _normalize_model_name(self, model: str) -> str:
        """Remove docker/ prefix."""
        if model.startswith("docker/"):
            return model[7:]
        return model
    
    async def complete(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Docker Model Runner completion."""
        normalized_model = self._normalize_model_name(model)
        url = f"{self.base_url}/v1/chat/completions"
        
        payload = {
            "model": normalized_model,
            "messages": messages,
            "stream": False
        }
        
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        
        try:
            response = await self.http_client.post(
                url,
                json=payload,
                headers=_auth_headers(self.api_key),
                timeout=LOCAL_MODEL_TIMEOUT
            )
            response.raise_for_status()
            return response.json()
        except httpx.ConnectError:
            raise httpx.HTTPStatusError(
                f"Cannot connect to Docker Model Runner. Enable it in Docker Desktop settings.",
                request=None,
                response=None
            )
    
    async def stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """Streaming Docker Model Runner completion."""
        normalized_model = self._normalize_model_name(model)
        url = f"{self.base_url}/v1/chat/completions"
        
        payload = {
            "model": normalized_model,
            "messages": messages,
            "stream": True
        }
        
        async with self.http_client.stream(
            "POST", 
            url, 
            json=payload,
            headers=_auth_headers(self.api_key),
            timeout=LOCAL_MODEL_TIMEOUT
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield line + "\n"
    
    @staticmethod
    def supports_model(model_name: str) -> bool:
        """Check if model is a Docker model."""
        return model_name.lower().startswith("docker/")
    
    def get_pricing(self, model: str) -> Dict[str, Decimal]:
        """Local models = zero cost."""
        return {
            "prompt_tokens_per_million": Decimal("0.00"),
            "completion_tokens_per_million": Decimal("0.00")
        }
    
    def calculate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int
    ) -> Decimal:
        """Local = zero cost."""
        return Decimal("0.00")
    
    def get_supported_models(self) -> List[str]:
        """Return list of Docker Model Runner models."""
        return ["llama3.2", "mistral", "phi3"]


# ============================================================================
# OPENAI-COMPATIBLE HTTP PROVIDER (Generic local/custom endpoint)
# ============================================================================


class OpenAICompatibleProvider(LLMProvider):
    """
    Generic OpenAI-compatible HTTP provider for local/custom endpoints.
    Expects a /v1/chat/completions route.
    """

    def __init__(
        self,
        api_key: str,
        http_client: httpx.AsyncClient,
        provider_name: str = "openai-compatible",
        base_url: Optional[str] = None,
    ):
        super().__init__(api_key, http_client, provider_name)
        self.base_url = base_url or "http://localhost:8000"
        self.log = log.bind(provider="openai_compatible", base_url=self.base_url)

    @staticmethod
    def _normalize_model_name(model: str) -> str:
        if model.startswith("local/"):
            return model[6:]
        return model

    async def complete(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        normalized_model = self._normalize_model_name(model)
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": normalized_model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        try:
            resp = await self.http_client.post(url, json=payload, headers=_auth_headers(self.api_key), timeout=LOCAL_MODEL_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException as e:
            self.log.error("local_provider_timeout", provider="openai_compatible", base_url=self.base_url)
            raise httpx.HTTPStatusError(
                "Local provider timeout",
                request=None,
                response=None,
            )

    @staticmethod
    def supports_model(model_name: str) -> bool:
        return model_name.lower().startswith("local/")

    def get_pricing(self, model: str) -> Dict[str, Decimal]:
        return {
            "prompt_tokens_per_million": Decimal("0.00"),
            "completion_tokens_per_million": Decimal("0.00"),
        }

    def calculate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> Decimal:
        return Decimal("0.00")

    def get_supported_models(self) -> List[str]:
        return ["local/model"]

    async def stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """Streaming via OpenAI-compatible endpoint."""
        normalized_model = self._normalize_model_name(model)
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": normalized_model,
            "messages": messages,
            "stream": True,
        }
        try:
            async with self.http_client.stream(
                "POST",
                url,
                json=payload,
                headers=_auth_headers(self.api_key),
                timeout=LOCAL_MODEL_TIMEOUT,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        yield line + "\n"
        except httpx.TimeoutException as e:
            self.log.error("local_provider_timeout", provider="openai_compatible", base_url=self.base_url, stream=True)
            raise httpx.HTTPStatusError(
                "Local provider timeout",
                request=None,
                response=None,
            )



class LocalEndpointProvider(OpenAICompatibleProvider):
    """`local/<model>`: the OpenAI-compatible server LOCAL_HTTP_BASE_URL names, Ollama's when unset."""

    def __init__(
        self,
        api_key: str,
        http_client: httpx.AsyncClient,
        provider_name: str = "local",
        base_url: Optional[str] = None,
    ):
        super().__init__(
            api_key, http_client, provider_name,
            base_url=base_url or os.getenv("LOCAL_HTTP_BASE_URL") or OLLAMA_BASE_URL,
        )


# ============================================================================
# LOCAL PROVIDER FACTORY
# ============================================================================

def get_local_provider(
    model: str,
    api_key: str,
    http_client: httpx.AsyncClient
) -> Optional[LLMProvider]:
    """
    Factory function to get the appropriate local provider for a model.
    
    Returns None if model is not a local model.
    """
    model_lower = model.lower()
    
    # Check Docker first (most specific prefix)
    if model_lower.startswith("docker/"):
        return DockerModelRunnerProvider(api_key, http_client)
    
    # Generic OpenAI-compatible local endpoint (env configured)
    if model_lower.startswith("local/") and LOCAL_HTTP_BASE_URL:
        return OpenAICompatibleProvider(api_key, http_client, base_url=LOCAL_HTTP_BASE_URL)
    
    # Check vLLM
    if model_lower.startswith("vllm/"):
        return VLLMProvider(api_key, http_client)
    
    # Check Ollama (including common local models)
    if OllamaProvider.supports_model(model):
        return OllamaProvider(api_key, http_client)
    
    # Generic "local/" prefix → default to Ollama
    if model_lower.startswith("local/"):
        return OllamaProvider(api_key, http_client)
    
    return None


def is_local_model(model: str) -> bool:
    """Check if model is any type of local model."""
    model_lower = model.lower()
    
    # Check all prefixes
    all_prefixes = OLLAMA_PREFIXES + VLLM_PREFIXES + LOCAL_PREFIXES + ["docker/"]
    for prefix in all_prefixes:
        if model_lower.startswith(prefix):
            return True
    
    return False

