"""
Multi-Provider Architecture for Oxy Engine.

This package implements the Strategy + Adapter pattern to support multiple LLM providers
(OpenAI, Anthropic, Google, Ollama, vLLM) while maintaining a consistent OpenAI-compatible public API.

Architecture:
1. base.py: Abstract LLMProvider interface + ProviderRegistry
2. pricing.py: Centralized pricing database for all providers
3. openai_provider.py: OpenAI API adapter
4. anthropic_provider.py: Anthropic (Claude) API adapter
5. google_provider.py: Google Gemini API adapter
6. local_provider.py: Local models (Ollama, vLLM, Docker Model Runner)

Usage:
    from providers import provider_registry, OpenAIProvider, AnthropicProvider, GoogleProvider
    
    # Register providers (already done at import)
    
    # Get provider for a model
    provider = provider_registry.get_provider_for_model("gpt-4", api_key, http_client)
    
    # Make request
    response = await provider.complete(model="gpt-4", messages=[...])
    
    # Local models work the same way
    provider = provider_registry.get_provider_for_model("ollama/llama3.2", api_key, http_client)
    response = await provider.complete(model="ollama/llama3.2", messages=[...])
"""

from .base import LLMProvider, ProviderRegistry, provider_registry, ProviderHTTPError
from .pricing import PricingEngine, calculate_cost, get_pricing, calculate_google_cost
from .openai_provider import OpenAIProvider, create_openai_provider
from .anthropic_provider import AnthropicProvider, create_anthropic_provider
from .google_provider import GoogleProvider
from .local_provider import (
    OllamaProvider, 
    VLLMProvider, 
    DockerModelRunnerProvider,
    LocalEndpointProvider,
    get_local_provider,
    is_local_model
)

# ============================================================================
# PROVIDER REGISTRATION
# ============================================================================
# Register all providers with their model prefixes.
# Order matters: more specific prefixes should come first.
# ============================================================================

# Cloud Providers
provider_registry.register("openai", OpenAIProvider, model_prefixes=[
    "gpt-", "o1-", "o3-", "text-", "davinci", "curie", "babbage", "ada"
])
provider_registry.register("anthropic", AnthropicProvider, model_prefixes=[
    "claude-"
])
provider_registry.register("google", GoogleProvider, model_prefixes=[
    "gemini-", "palm-"
])

# Local Providers (AGENT SUPPORT)
provider_registry.register("ollama", OllamaProvider, model_prefixes=[
    "ollama/", "llama", "mistral", "codellama", "phi", "gemma", "qwen", "deepseek", "vicuna", "orca"
])
provider_registry.register("vllm", VLLMProvider, model_prefixes=[
    "vllm/"
])
provider_registry.register("docker", DockerModelRunnerProvider, model_prefixes=[
    "docker/"
])

# Generic local prefix: LOCAL_HTTP_BASE_URL, Ollama's address when unset
provider_registry.register("local", LocalEndpointProvider, model_prefixes=[
    "local/"
])


__all__ = [
    # Base classes
    "LLMProvider",
    "ProviderRegistry",
    "provider_registry",
    "ProviderHTTPError",
    
    # Pricing
    "PricingEngine",
    "calculate_cost",
    "get_pricing",
    "calculate_google_cost",
    
    # Cloud Providers
    "OpenAIProvider",
    "create_openai_provider",
    "AnthropicProvider",
    "create_anthropic_provider",
    "GoogleProvider",
    
    # Local Providers (AGENT SUPPORT)
    "OllamaProvider",
    "LocalEndpointProvider",
    "VLLMProvider",
    "DockerModelRunnerProvider",
    "get_local_provider",
    "is_local_model",
]

