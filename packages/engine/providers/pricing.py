"""
Universal Pricing Engine for Multi-Provider Architecture.

This module centralizes pricing information for all LLM providers.
Prices are per 1M tokens (both prompt and completion).

Pricing data sources:
- OpenAI: https://openai.com/api/pricing/
- Anthropic: https://www.anthropic.com/pricing
- Google: https://ai.google.dev/pricing

Last updated: November 2025
"""

import os
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional
import structlog

log = structlog.get_logger()


class PricingEngine:
    """
    Centralized pricing database for all LLM providers.
    
    Responsibilities:
    - Store pricing per model (prompt tokens, completion tokens)
    - Calculate cost for a given usage
    - Support dynamic pricing updates (future: from database)
    
    Pricing is stored in USD per 1 million tokens for precision.
    """
    
    # Pricing database: model_name → {prompt_price, completion_price}
    # Prices are per 1M tokens in USD
    PRICING_TABLE: Dict[str, Dict[str, Decimal]] = {
        # ========== OPENAI ==========
        
        # GPT-4 Turbo (latest, most cost-effective GPT-4)
        "gpt-4-turbo": {
            "prompt_tokens_per_million": Decimal("10.00"),
            "completion_tokens_per_million": Decimal("30.00"),
        },
        # GPT-4o family (latest OpenAI general models)
        "gpt-4o": {
            "prompt_tokens_per_million": Decimal("5.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },
        "gpt-4o-mini": {
            "prompt_tokens_per_million": Decimal("0.15"),
            "completion_tokens_per_million": Decimal("0.60"),
        },
        "gpt-4-turbo-preview": {
            "prompt_tokens_per_million": Decimal("10.00"),
            "completion_tokens_per_million": Decimal("30.00"),
        },
        "gpt-4-0125-preview": {
            "prompt_tokens_per_million": Decimal("10.00"),
            "completion_tokens_per_million": Decimal("30.00"),
        },
        "gpt-4-1106-preview": {
            "prompt_tokens_per_million": Decimal("10.00"),
            "completion_tokens_per_million": Decimal("30.00"),
        },
        
        # GPT-4 (original, more expensive)
        "gpt-4": {
            "prompt_tokens_per_million": Decimal("30.00"),
            "completion_tokens_per_million": Decimal("60.00"),
        },
        "gpt-4-0613": {
            "prompt_tokens_per_million": Decimal("30.00"),
            "completion_tokens_per_million": Decimal("60.00"),
        },
        "gpt-4-32k": {
            "prompt_tokens_per_million": Decimal("60.00"),
            "completion_tokens_per_million": Decimal("120.00"),
        },
        "gpt-4-32k-0613": {
            "prompt_tokens_per_million": Decimal("60.00"),
            "completion_tokens_per_million": Decimal("120.00"),
        },
        
        # GPT-3.5 Turbo (cheapest, fastest)
        "gpt-3.5-turbo": {
            "prompt_tokens_per_million": Decimal("0.50"),
            "completion_tokens_per_million": Decimal("1.50"),
        },
        "gpt-3.5-turbo-0125": {
            "prompt_tokens_per_million": Decimal("0.50"),
            "completion_tokens_per_million": Decimal("1.50"),
        },
        "gpt-3.5-turbo-1106": {
            "prompt_tokens_per_million": Decimal("1.00"),
            "completion_tokens_per_million": Decimal("2.00"),
        },
        "gpt-3.5-turbo-16k": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("4.00"),
        },
        
        # o1 series (reasoning models, expensive)
        "o1-preview": {
            "prompt_tokens_per_million": Decimal("15.00"),
            "completion_tokens_per_million": Decimal("60.00"),
        },
        "o1-mini": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("12.00"),
        },
        
        # ========== ANTHROPIC ==========
        # Prices in USD per 1M tokens (prompt / completion).
        # Sourced from anthropic.com/pricing; update on each model release.

        # ----- Claude 4.6 (current generation as of 2026-05) -----
        "claude-haiku-4-6": {
            "prompt_tokens_per_million": Decimal("1.00"),
            "completion_tokens_per_million": Decimal("5.00"),
        },
        "claude-sonnet-4-6": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },
        "claude-opus-4-6": {
            "prompt_tokens_per_million": Decimal("15.00"),
            "completion_tokens_per_million": Decimal("75.00"),
        },

        # ----- Claude 4.5 -----
        "claude-haiku-4-5-20251001": {
            "prompt_tokens_per_million": Decimal("1.00"),
            "completion_tokens_per_million": Decimal("5.00"),
        },
        "claude-haiku-4-5": {
            "prompt_tokens_per_million": Decimal("1.00"),
            "completion_tokens_per_million": Decimal("5.00"),
        },
        "claude-sonnet-4-5-20250929": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },
        "claude-sonnet-4-5": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },

        # ----- Claude 4.1 (opus refresh) -----
        "claude-opus-4-1-20250805": {
            "prompt_tokens_per_million": Decimal("15.00"),
            "completion_tokens_per_million": Decimal("75.00"),
        },
        "claude-opus-4-1": {
            "prompt_tokens_per_million": Decimal("15.00"),
            "completion_tokens_per_million": Decimal("75.00"),
        },

        # ----- Claude 4 (initial 4-series, May 2025) -----
        "claude-sonnet-4-20250514": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },
        "claude-sonnet-4": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },
        "claude-opus-4-20250514": {
            "prompt_tokens_per_million": Decimal("15.00"),
            "completion_tokens_per_million": Decimal("75.00"),
        },
        "claude-opus-4": {
            "prompt_tokens_per_million": Decimal("15.00"),
            "completion_tokens_per_million": Decimal("75.00"),
        },

        # ----- Claude 3.7 Sonnet -----
        "claude-3-7-sonnet-20250219": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },
        "claude-3-7-sonnet": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },

        # ----- Claude 3.5 Haiku -----
        "claude-3-5-haiku-20241022": {
            "prompt_tokens_per_million": Decimal("0.80"),
            "completion_tokens_per_million": Decimal("4.00"),
        },
        "claude-3-5-haiku": {
            "prompt_tokens_per_million": Decimal("0.80"),
            "completion_tokens_per_million": Decimal("4.00"),
        },

        # ----- Claude 3.5 Sonnet -----
        "claude-3-5-sonnet-20241022": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },
        "claude-3-5-sonnet-20240620": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },
        "claude-3-5-sonnet": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },

        # ----- Claude 3 (legacy) -----
        "claude-3-opus-20240229": {
            "prompt_tokens_per_million": Decimal("15.00"),
            "completion_tokens_per_million": Decimal("75.00"),
        },
        "claude-3-sonnet-20240229": {
            "prompt_tokens_per_million": Decimal("3.00"),
            "completion_tokens_per_million": Decimal("15.00"),
        },
        "claude-3-haiku-20240307": {
            "prompt_tokens_per_million": Decimal("0.25"),
            "completion_tokens_per_million": Decimal("1.25"),
        },
        
        # ========== GOOGLE GEMINI ==========
        
        # Gemini 1.5 Pro (latest)
        "gemini-1.5-pro": {
            "prompt_tokens_per_million": Decimal("3.50"),
            "completion_tokens_per_million": Decimal("10.50"),
        },
        "gemini-1.5-pro-latest": {
            "prompt_tokens_per_million": Decimal("3.50"),
            "completion_tokens_per_million": Decimal("10.50"),
        },
        
        # Gemini 1.5 Flash (fast, cheap)
        "gemini-1.5-flash": {
            "prompt_tokens_per_million": Decimal("0.35"),
            "completion_tokens_per_million": Decimal("1.05"),
        },
        "gemini-1.5-flash-latest": {
            "prompt_tokens_per_million": Decimal("0.35"),
            "completion_tokens_per_million": Decimal("1.05"),
        },
        
        # Gemini 1.0 Pro (legacy)
        "gemini-1.0-pro": {
            "prompt_tokens_per_million": Decimal("0.50"),
            "completion_tokens_per_million": Decimal("1.50"),
        },
    }
    
    # Default pricing for unknown models (conservative estimate)
    DEFAULT_PRICING = {
        "prompt_tokens_per_million": Decimal("10.00"),
        "completion_tokens_per_million": Decimal("30.00"),
    }
    
    # LEGACY ALIAS: Some modules reference PRICES instead of PRICING_TABLE
    # Format: model_name → (prompt_price_per_token, completion_price_per_token)
    # This is the OLD format used by main.py and cache_warmer.py
    PRICES = {
        # OpenAI
        "gpt-4": (0.00003, 0.00006),  # $30/$60 per 1M
        "gpt-4-turbo": (0.00001, 0.00003),  # $10/$30 per 1M
        "gpt-4-turbo-preview": (0.00001, 0.00003),
        "gpt-4-0125-preview": (0.00001, 0.00003),
        "gpt-4-1106-preview": (0.00001, 0.00003),
        "gpt-4-32k": (0.00006, 0.00012),
        "gpt-3.5-turbo": (0.0000005, 0.0000015),  # $0.50/$1.50 per 1M
        "gpt-3.5-turbo-0125": (0.0000005, 0.0000015),
        "gpt-3.5-turbo-16k": (0.000003, 0.000004),
        "o1-preview": (0.000015, 0.00006),
        "o1-mini": (0.000003, 0.000012),
        # Anthropic — Claude 4.6 (current, May 2026)
        "claude-haiku-4-6":          (0.000001,  0.000005),  # $1 / $5
        "claude-sonnet-4-6":         (0.000003,  0.000015),  # $3 / $15
        "claude-opus-4-6":           (0.000015,  0.000075),  # $15 / $75
        # Anthropic — Claude 4.5
        "claude-haiku-4-5":          (0.000001,  0.000005),  # $1 / $5
        "claude-haiku-4-5-20251001": (0.000001,  0.000005),
        "claude-sonnet-4-5":         (0.000003,  0.000015),  # $3 / $15
        "claude-sonnet-4-5-20250929":(0.000003,  0.000015),
        # Anthropic — Claude 4.1 opus
        "claude-opus-4-1":           (0.000015,  0.000075),  # $15 / $75
        "claude-opus-4-1-20250805":  (0.000015,  0.000075),
        # Anthropic — Claude 4 (initial)
        "claude-sonnet-4":           (0.000003,  0.000015),
        "claude-sonnet-4-20250514":  (0.000003,  0.000015),
        "claude-opus-4":             (0.000015,  0.000075),
        "claude-opus-4-20250514":    (0.000015,  0.000075),
        # Anthropic — Claude 3.7
        "claude-3-7-sonnet":         (0.000003,  0.000015),
        "claude-3-7-sonnet-20250219":(0.000003,  0.000015),
        # Anthropic — Claude 3.5
        "claude-3-5-haiku":          (0.0000008, 0.000004),  # $0.80 / $4
        "claude-3-5-haiku-20241022": (0.0000008, 0.000004),
        "claude-3-5-sonnet":         (0.000003,  0.000015),
        "claude-3-5-sonnet-20241022":(0.000003,  0.000015),
        "claude-3-5-sonnet-20240620":(0.000003,  0.000015),
        # Anthropic — Claude 3 (legacy)
        "claude-3-opus":             (0.000015,  0.000075),
        "claude-3-opus-20240229":    (0.000015,  0.000075),
        "claude-3-sonnet":           (0.000003,  0.000015),
        "claude-3-sonnet-20240229":  (0.000003,  0.000015),
        "claude-3-haiku":            (0.00000025,0.00000125),
        "claude-3-haiku-20240307":   (0.00000025,0.00000125),
        # Google Gemini
        "gemini-1.5-pro": (0.0000035, 0.0000105),
        "gemini-1.5-pro-latest": (0.0000035, 0.0000105),
        "gemini-1.5-flash": (0.00000035, 0.00000105),
        "gemini-1.5-flash-latest": (0.00000035, 0.00000105),
        "gemini-1.0-pro": (0.0000005, 0.0000015),
    }
    
    @classmethod
    def get_pricing(cls, model: str) -> Dict[str, Decimal]:
        """
        Get pricing for a model.
        
        Args:
            model: Model name (e.g., "gpt-4", "claude-3-opus")
        
        Returns:
            Dict with pricing per 1M tokens:
            {
                "prompt_tokens_per_million": Decimal("5.00"),
                "completion_tokens_per_million": Decimal("15.00")
            }
        
        If model not found, returns conservative default pricing.
        """
        if model in cls.PRICING_TABLE:
            return cls.PRICING_TABLE[model]
        
        # A model served by the operator's own hardware bills nothing per token
        # unless the operator says what a million tokens cost them (for example
        # the GPU's amortized cost), so a budget can still cap a local model.
        from .local_provider import is_local_model
        if is_local_model(model):
            return cls.local_pricing()
        
        # Log unknown model for monitoring
        log.warning(
            "unknown_model_pricing",
            model=model,
            using_default=True,
            default_prompt_price=str(cls.DEFAULT_PRICING["prompt_tokens_per_million"]),
            default_completion_price=str(cls.DEFAULT_PRICING["completion_tokens_per_million"])
        )
        
        return cls.DEFAULT_PRICING
    
    @staticmethod
    def local_pricing() -> Dict[str, Decimal]:
        """Per-million-token price of a local model: LOCAL_PROMPT_PRICE_PER_MILLION and
        LOCAL_COMPLETION_PRICE_PER_MILLION in USD, 0 when unset or not a number."""
        def price(name: str) -> Decimal:
            try:
                return max(Decimal(os.getenv(name, "0").strip() or "0"), Decimal("0"))
            except InvalidOperation:
                return Decimal("0")
        return {
            "prompt_tokens_per_million": price("LOCAL_PROMPT_PRICE_PER_MILLION"),
            "completion_tokens_per_million": price("LOCAL_COMPLETION_PRICE_PER_MILLION"),
        }

    @classmethod
    def calculate_cost(
        cls,
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
        
        Formula:
            cost = (prompt_tokens / 1M * prompt_price) + (completion_tokens / 1M * completion_price)
        
        Example:
            >>> PricingEngine.calculate_cost("gpt-4", 1000, 500)
            Decimal("0.06")  # $0.06 = 6 cents
            
            Breakdown:
            - Prompt: 1000 tokens / 1M * $30 = $0.03
            - Completion: 500 tokens / 1M * $60 = $0.03
            - Total: $0.06
        """
        pricing = cls.get_pricing(model)
        
        # Calculate cost with high precision (Decimal arithmetic)
        prompt_cost = (
            Decimal(str(prompt_tokens)) / Decimal("1000000")
        ) * pricing["prompt_tokens_per_million"]
        
        completion_cost = (
            Decimal(str(completion_tokens)) / Decimal("1000000")
        ) * pricing["completion_tokens_per_million"]
        
        total_cost = prompt_cost + completion_cost
        
        # Round to 6 decimal places (sub-cent precision)
        return total_cost.quantize(Decimal("0.000001"))
    
    @classmethod
    def get_supported_models(cls) -> list[str]:
        """
        Get list of all models with known pricing.
        
        Returns:
            List of model names
        """
        return list(cls.PRICING_TABLE.keys())
    
    @classmethod
    def get_models_by_provider(cls, provider: str) -> list[str]:
        """
        Get models for a specific provider.
        
        Args:
            provider: Provider name ("openai", "anthropic", "google")
        
        Returns:
            List of model names for that provider
        """
        # Simple heuristic: model name prefixes
        prefixes = {
            "openai": ["gpt-", "o1-", "text-"],
            "anthropic": ["claude-"],
            "google": ["gemini-"],
        }
        
        provider_prefixes = prefixes.get(provider, [])
        
        return [
            model
            for model in cls.PRICING_TABLE.keys()
            if any(model.startswith(prefix) for prefix in provider_prefixes)
        ]
    
    @classmethod
    def estimate_max_cost_for_model(cls, model: str, max_tokens: int = 4000) -> Decimal:
        """
        Estimate maximum cost for a request (used for budget reservation).
        
        Conservative estimate assuming:
        - Prompt: max_tokens / 2
        - Completion: max_tokens / 2
        
        Args:
            model: Model name
            max_tokens: Maximum total tokens (default: 4000)
        
        Returns:
            Estimated maximum cost in USD
        
        Example:
            >>> PricingEngine.estimate_max_cost_for_model("gpt-4", 4000)
            Decimal("0.18")  # $0.18 for 4k tokens (2k prompt + 2k completion)
        """
        pricing = cls.get_pricing(model)
        
        # Conservative estimate: assume 50/50 split
        estimated_prompt_tokens = max_tokens // 2
        estimated_completion_tokens = max_tokens // 2
        
        return cls.calculate_cost(model, estimated_prompt_tokens, estimated_completion_tokens)


# Convenience function for direct import
def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
    """Convenience wrapper for PricingEngine.calculate_cost()"""
    return PricingEngine.calculate_cost(model, prompt_tokens, completion_tokens)


def get_pricing(model: str) -> Dict[str, Decimal]:
    """Convenience wrapper for PricingEngine.get_pricing()"""
    return PricingEngine.get_pricing(model)


# ============================================================================
# GOOGLE GEMINI TIERED PRICING
# ============================================================================
# Google has context-based pricing tiers:
# - <=128k tokens: Base rate
# - >128k tokens: 2× base rate
# 
# This dramatically impacts profitability at scale. A 1M token request on
# Gemini 1.5 Pro costs $21 (output), not $10.50. Our pricing engine MUST
# handle this or we'll lose 50% margin on long-context requests.
# ============================================================================

def calculate_google_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """
    Calculate cost for Google Gemini models with tiered pricing.
    
    Google's pricing structure (as of Nov 2024):
    
    **Gemini 1.5 Flash:**
    - Input <=128k: $0.075/1M, >128k: $0.15/1M
    - Output <=128k: $0.30/1M, >128k: $0.60/1M
    
    **Gemini 1.5 Pro:**
    - Input <=128k: $3.50/1M, >128k: $7.00/1M
    - Output <=128k: $10.50/1M, >128k: $21.00/1M
    
    **Gemini 1.0 Pro:**
    - Flat rate: $0.50/1M input, $1.50/1M output
    
    Args:
        model: Gemini model name (e.g., "gemini-1.5-pro")
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
    
    Returns:
        Total cost in USD (Decimal for precision)
    
    Example:
        >>> calculate_google_cost("gemini-1.5-pro", 200000, 50000)
        Decimal("2.45")  # $1.40 (input) + $1.05 (output)
        
        Breakdown:
        - Input: 128k @ $3.50/1M + 72k @ $7.00/1M = $0.448 + $0.504 = $0.952
        - Output: 50k @ $10.50/1M = $0.525
        - Total: $1.477 (rounded to $1.48)
    
    Why it matters:
        Without tiered pricing logic, a 1M token request would be under-counted
        by about 50% in cost tracking and budgets.
    """
    TIER_THRESHOLD = 128_000  # 128k tokens
    
    # Pricing tables per model (per 1M tokens)
    TIERED_PRICING = {
        # Gemini 1.5 Flash
        "gemini-1.5-flash": {
            "input_base": Decimal("0.075"),
            "input_extended": Decimal("0.15"),
            "output_base": Decimal("0.30"),
            "output_extended": Decimal("0.60"),
        },
        "gemini-1.5-flash-latest": {
            "input_base": Decimal("0.075"),
            "input_extended": Decimal("0.15"),
            "output_base": Decimal("0.30"),
            "output_extended": Decimal("0.60"),
        },
        # Gemini 1.5 Pro
        "gemini-1.5-pro": {
            "input_base": Decimal("3.50"),
            "input_extended": Decimal("7.00"),
            "output_base": Decimal("10.50"),
            "output_extended": Decimal("21.00"),
        },
        "gemini-1.5-pro-latest": {
            "input_base": Decimal("3.50"),
            "input_extended": Decimal("7.00"),
            "output_base": Decimal("10.50"),
            "output_extended": Decimal("21.00"),
        },
    }
    
    # Check if model has tiered pricing
    if model not in TIERED_PRICING:
        # Fall back to standard pricing for Gemini 1.0 Pro and unknown models
        log.info(
            "google_using_flat_rate_pricing",
            model=model,
            reason="Model not in tiered pricing table"
        )
        return PricingEngine.calculate_cost(model, input_tokens, output_tokens)
    
    pricing = TIERED_PRICING[model]
    
    # Calculate input cost with tiers
    if input_tokens <= TIER_THRESHOLD:
        # All tokens at base rate
        input_cost = (Decimal(str(input_tokens)) / Decimal("1000000")) * pricing["input_base"]
    else:
        # First 128k at base rate, rest at extended rate
        base_tokens = TIER_THRESHOLD
        extended_tokens = input_tokens - TIER_THRESHOLD
        
        input_cost = (
            (Decimal(str(base_tokens)) / Decimal("1000000")) * pricing["input_base"] +
            (Decimal(str(extended_tokens)) / Decimal("1000000")) * pricing["input_extended"]
        )
    
    # Calculate output cost with tiers
    if output_tokens <= TIER_THRESHOLD:
        # All tokens at base rate
        output_cost = (Decimal(str(output_tokens)) / Decimal("1000000")) * pricing["output_base"]
    else:
        # First 128k at base rate, rest at extended rate
        base_tokens = TIER_THRESHOLD
        extended_tokens = output_tokens - TIER_THRESHOLD
        
        output_cost = (
            (Decimal(str(base_tokens)) / Decimal("1000000")) * pricing["output_base"] +
            (Decimal(str(extended_tokens)) / Decimal("1000000")) * pricing["output_extended"]
        )
    
    total_cost = input_cost + output_cost
    
    # Round to 6 decimal places (sub-cent precision)
    total_cost = total_cost.quantize(Decimal("0.000001"))
    
    log.info(
        "google_tiered_cost_calculated",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_tier="base" if input_tokens <= TIER_THRESHOLD else "extended",
        output_tier="base" if output_tokens <= TIER_THRESHOLD else "extended",
        total_cost_usd=float(total_cost)
    )
    
    return total_cost

