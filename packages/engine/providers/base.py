"""
Base Provider Interface for Multi-Provider Architecture.

This module defines the abstract interface that all LLM providers must implement.
Ensures consistent behavior across OpenAI, Anthropic, Google, and future providers.

Architecture Pattern: Strategy Pattern + Adapter Pattern
- Strategy: Choose provider at runtime based on model name
- Adapter: Convert OpenAI-format requests to provider-specific format
"""

from abc import ABC, abstractmethod
from typing import AsyncGenerator, Dict, Any, Optional, List
from decimal import Decimal
import httpx
import structlog

log = structlog.get_logger()


class ProviderHTTPError(Exception):
    """
    Structured provider HTTP error to surface status codes and safe messages.
    
    Attributes:
        status_code: Upstream HTTP status
        provider: Provider name (e.g., "openai")
        message: Short safe message
        safe_body: Truncated/sanitized response body (optional)
    """

    def __init__(self, status_code: int, provider: str, message: str, safe_body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.provider = provider
        self.message = message
        self.safe_body = safe_body


class LLMProvider(ABC):
    """
    Abstract base class for all LLM providers.
    
    All providers (OpenAI, Anthropic, Google, etc.) must implement this interface
    to ensure consistent behavior and seamless switching.
    
    The public API (`/v1/chat/completions`) always receives and returns OpenAI format.
    Each provider adapter is responsible for:
    1. Converting OpenAI format → Provider-specific format
    2. Making the API call to the provider
    3. Converting Provider-specific response → OpenAI format
    """
    
    def __init__(
        self,
        api_key: str,
        http_client: httpx.AsyncClient,
        provider_name: str
    ):
        """
        Initialize provider.
        
        Args:
            api_key: Provider API key (decrypted from Supabase Vault)
            http_client: Shared async HTTP client (with timeouts, retry logic)
            provider_name: Provider identifier (e.g., "openai", "anthropic")
        """
        self.api_key = api_key
        self.http_client = http_client
        self.provider_name = provider_name
        self.log = log.bind(provider=provider_name)
    
    @abstractmethod
    async def complete(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Non-streaming completion request.
        
        Args:
            model: Model name (e.g., "gpt-4", "claude-3-opus")
            messages: List of message dicts in OpenAI format:
                      [{"role": "user", "content": "Hello"}]
            temperature: Sampling temperature (0.0-2.0)
            max_tokens: Maximum tokens to generate
            **kwargs: Additional provider-specific parameters
        
        Returns:
            OpenAI-format response dict:
            {
                "id": "chatcmpl-...",
                "object": "chat.completion",
                "created": 1234567890,
                "model": "gpt-4",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "..."},
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30
                }
            }
        
        Raises:
            httpx.HTTPStatusError: On provider API errors
            Exception: On conversion/parsing errors
        """
        pass
    
    @abstractmethod
    async def stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        Streaming completion request.
        
        Args:
            model: Model name
            messages: Messages in OpenAI format
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            **kwargs: Additional provider-specific parameters
        
        Yields:
            Server-Sent Events (SSE) strings in OpenAI format:
            "data: {chunk}\n\n"
            
            Where chunk is:
            {
                "id": "chatcmpl-...",
                "object": "chat.completion.chunk",
                "created": 1234567890,
                "model": "gpt-4",
                "choices": [{
                    "index": 0,
                    "delta": {"content": "word"},
                    "finish_reason": null
                }]
            }
            
            Last chunk:
            "data: [DONE]\n\n"
        
        Raises:
            httpx.HTTPStatusError: On provider API errors
            Exception: On conversion/parsing errors
        """
        pass
    
    @abstractmethod
    def get_pricing(self, model: str) -> Dict[str, Decimal]:
        """
        Get pricing information for a model.
        
        Args:
            model: Model name (e.g., "gpt-4-turbo", "claude-3-opus")
        
        Returns:
            Dict with pricing per 1M tokens:
            {
                "prompt_tokens_per_million": Decimal("5.00"),
                "completion_tokens_per_million": Decimal("15.00")
            }
        
        Raises:
            ValueError: If model is not supported by this provider
        """
        pass
    
    @abstractmethod
    def calculate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int
    ) -> Decimal:
        """
        Calculate total cost for a request.
        
        Args:
            model: Model name
            prompt_tokens: Number of prompt tokens
            completion_tokens: Number of completion tokens
        
        Returns:
            Total cost in USD (Decimal for precision)
        
        Example:
            >>> provider.calculate_cost("gpt-4", 1000, 500)
            Decimal("0.0275")  # $0.0275 = 2.75 cents
        """
        pass
    
    @abstractmethod
    def get_supported_models(self) -> list[str]:
        """
        Get list of models supported by this provider.
        
        Returns:
            List of model names (e.g., ["gpt-4", "gpt-4-turbo", "gpt-3.5-turbo"])
        
        Used for:
        - Model validation before making API calls
        - UI dropdowns for model selection
        - Pricing lookups
        """
        pass
    
    def validate_model(self, model: str) -> bool:
        """
        Check if model is supported by this provider.
        
        Args:
            model: Model name to validate
        
        Returns:
            True if supported, False otherwise
        """
        return model in self.get_supported_models()


class ProviderRegistry:
    """
    Registry for managing multiple LLM providers.
    
    Responsibilities:
    - Map model names to providers (e.g., "gpt-4" → OpenAIProvider)
    - Lazy-load providers only when needed
    - Cache provider instances for reuse
    
    Usage:
        registry = ProviderRegistry()
        registry.register("openai", OpenAIProvider)
        registry.register("anthropic", AnthropicProvider)
        
        provider = await registry.get_provider_for_model("gpt-4", api_keys)
        response = await provider.complete(model="gpt-4", messages=[...])
    """
    
    MAX_KEYED_INSTANCES = 256

    def __init__(self):
        self._provider_classes: Dict[str, type[LLMProvider]] = {}
        # Instances installed by hand (tests, a host application), by provider name.
        self._provider_instances: Dict[str, LLMProvider] = {}
        # Instances the registry built, by provider name, then API key: an instance
        # holds the key it was built with, so one per name made every
        # organization's request leave with the first organization's key.
        self._keyed_instances: Dict[str, Dict[str, LLMProvider]] = {}
        self._model_to_provider: Dict[str, str] = {}
        # Initialize logger to prevent AttributeError
        self.log = structlog.get_logger().bind(component="provider_registry")
    
    def register(
        self,
        provider_name: str,
        provider_class: type[LLMProvider],
        model_prefixes: Optional[list[str]] = None
    ):
        """
        Register a provider class.
        
        Args:
            provider_name: Provider identifier (e.g., "openai", "anthropic")
            provider_class: Provider class (must inherit from LLMProvider)
            model_prefixes: Optional list of model name prefixes
                           (e.g., ["gpt-", "text-"] for OpenAI)
        """
        self._provider_classes[provider_name] = provider_class
        
        if model_prefixes:
            for prefix in model_prefixes:
                self._model_to_provider[prefix] = provider_name
        
        self.log.info(
            "provider_registered",
            provider=provider_name,
            prefixes=model_prefixes
        )
    
    def get_provider_for_model(
        self,
        model: str,
        api_key: str,
        http_client: httpx.AsyncClient
    ) -> LLMProvider:
        """
        Get provider instance for a given model.
        
        Args:
            model: Model name (e.g., "gpt-4", "claude-3-opus")
            api_key: Provider API key
            http_client: HTTP client for API calls
        
        Returns:
            Provider instance (OpenAIProvider, AnthropicProvider, etc.)
        
        Raises:
            ValueError: If no provider found for model
        """
        # Determine provider from model name
        provider_name = self._determine_provider(model)
        
        if provider_name not in self._provider_classes:
            raise ValueError(
                f"No provider registered for model '{model}'. "
                f"Available providers: {list(self._provider_classes.keys())}"
            )
        
        if provider_name in self._provider_instances:
            return self._provider_instances[provider_name]

        # Lazy-instantiate provider, one per provider name and API key. The key
        # is only ever the dict key of a per-provider mapping and the `api_key`
        # argument: no other local variable holds it, so an error reporter that
        # captures locals and filters them by name (it filters `api_key`) sees
        # nothing it would not have seen before.
        by_key = self._keyed_instances.setdefault(provider_name, {})
        instance = by_key.get(api_key or "")
        if instance is None:
            if len(by_key) >= self.MAX_KEYED_INSTANCES:
                by_key.pop(next(iter(by_key)))
            provider_class = self._provider_classes[provider_name]
            instance = by_key[api_key or ""] = provider_class(
                api_key=api_key,
                http_client=http_client,
                provider_name=provider_name
            )
        return instance
    
    def get_provider_name_for_model(self, model: str) -> str:
        """
        Get provider name for a model WITHOUT instantiating the provider.
        
        Use this when you need to know which provider handles a model
        before you have the API key (e.g., to fetch the correct key).
        
        Args:
            model: Model name (e.g., "gpt-4", "claude-3-opus")
        
        Returns:
            Provider name (e.g., "openai", "anthropic", "google")
        """
        return self._determine_provider(model)
    
    def _determine_provider(self, model: str) -> str:
        """
        Determine which provider handles a given model.
        
        Logic:
        1. Check exact match in model_to_provider mapping
        2. Check prefix match (e.g., "gpt-" → "openai")
        3. Fall back to default provider (openai for now)
        
        Args:
            model: Model name
        
        Returns:
            Provider name
        """
        # Exact match
        if model in self._model_to_provider:
            return self._model_to_provider[model]
        
        # Prefix match
        for prefix, provider_name in self._model_to_provider.items():
            if model.startswith(prefix):
                return provider_name
        
        # Default to OpenAI (for backward compatibility)
        # Most models without explicit prefix are OpenAI models
        return "openai"


# Global registry instance (initialized in main.py)
provider_registry = ProviderRegistry()

