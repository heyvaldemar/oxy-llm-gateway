import os
import re
import httpx
import hashlib
import secrets
import json
import asyncio
import redis.asyncio as aioredis
import html
import uuid
import jwt
import structlog
from contextvars import ContextVar
from typing import TypedDict, Dict, Tuple, Optional, List, Union
from fastapi import FastAPI, Request, Response, Depends, BackgroundTasks, HTTPException, Query, status
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator, condecimal, ValidationError
from contextlib import asynccontextmanager
from decimal import Decimal
from datetime import datetime, timezone
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

# Import shared resources from core.py to break circular dependency
# Previously main.py <-> billing.py caused ImportError on startup
import core
from core import (
    log,
    resources,  # ResourceManager instance - access clients via resources.redis, etc.
    SUPABASE_URL,
    SUPABASE_KEY,
    REDIS_URL,
    ENVIRONMENT,
    KEY_CACHE_TTL,
    KEY_PREFIX_LENGTH,
    KEY_PREFIX_BYTES,
    KEY_SUFFIX_BYTES,
    OPENAI_PROVIDER_NAME,
    MAX_REQUEST_SIZE,
    MAX_MESSAGE_LENGTH,
    MAX_MESSAGES,
    REQUEST_TIMEOUT,
    NON_STREAMING_TIMEOUT,
    RATE_LIMIT_PER_MINUTE,
    RATE_LIMIT_MANAGEMENT_PER_MINUTE,
    AUTH_RATE_LIMIT_PER_MINUTE,
    DLP_PATTERNS,
)
from core.addons import get_org_add_ons, compute_project_limit
from core.response_cache import (
    ResponseCache,
    RESPONSE_CACHE_TTL as DETERMINISTIC_RESPONSE_CACHE_TTL,
)

# Import security utilities
from security_utils import SecretString, log_error_safe, CacheEncryption
from redis_scripts import parse_rate_limit_result, LUA_RATE_LIMIT_CHECK
# MULTI-PROVIDER ARCHITECTURE: Import provider registry and adapters
from providers import provider_registry, OpenAIProvider, AnthropicProvider, PricingEngine

# Import refactored middleware
from middleware import (
    add_security_headers,
    csrf_protection_middleware,
    auth_rate_limit_middleware,
    global_rate_limit_middleware,
    add_rate_limit_headers_middleware,
    key_rotation_middleware,
    add_request_id_middleware,
    limit_request_size,
    geo_blocking_middleware,
)

# REFACTORING PHASE 1: Import modular dependencies and routers
# These modules break circular imports and enable parallel development
from dependencies import (
    get_current_user_id,
    get_user_and_key_from_api_key,
    CacheData,
    get_supabase_headers,
    hash_key,
    api_key_cache_name_for_hash,
    project_keys_set_name,
    forget_cached_api_key,
)
from routes.auth import router as auth_router
from routes.add_ons import router as add_ons_router
from routes.cost_optimizer import router as cost_optimizer_router
from routes.prompts import router as prompts_router
from routes.intelligence import router as intelligence_router
from routes.team import router as team_router

# CRITICAL: Request ID tracing for distributed logging
# Enables correlation between frontend errors and backend logs
request_id_ctx: ContextVar[str] = ContextVar('request_id', default='')

# Prometheus metrics for monitoring
metrics_requests_total = Counter(
    'oxy_requests_total', 
    'Total API requests', 
    ['status', 'model', 'endpoint']
)
metrics_request_duration = Histogram(
    'oxy_request_duration_seconds', 
    'Request duration in seconds',
    ['endpoint']
)
metrics_tokens_total = Counter(
    'oxy_tokens_total', 
    'Total tokens processed', 
    ['model', 'type']  # type: prompt or completion
)
metrics_cost_dollars_total = Counter(
    'oxy_cost_dollars_total', 
    'Total cost in USD', 
    ['model']
)
metrics_threats_blocked_total = Counter(
    'oxy_threats_blocked_total', 
    'Total threats blocked', 
    ['threat_type']
)
metrics_active_subscriptions = Gauge(
    'oxy_active_subscriptions', 
    'Number of active subscriptions',
    ['plan_tier']
)
metrics_budget_exceeded = Counter(
    'oxy_budget_exceeded_total', 
    'Budget exceeded events', 
    ['project_id']
)

# NOTE: Environment variables and shared clients are now imported from core.py
# This avoids circular dependency with billing.py

auth_scheme = HTTPBearer()

OPENAI_KEY_REGEX = re.compile(r'sk(?:-proj)?-[A-Za-z0-9_-]{40,}')
JWT_EXPIRY_TOLERANCE_SECONDS = 60  # JWT clock skew tolerance
CSRF_TOKEN_HEADER = "X-CSRF-Token"  # CSRF protection (HIGH #4 fix)
CSRF_TOKEN_COOKIE = "csrf_token"

# ============================================================================
# CSRF PROTECTION: PATHS THAT REQUIRE CSRF VALIDATION
# ============================================================================
# Public API endpoints (API key auth) are excluded from CSRF validation
CSRF_EXEMPT_PATHS = {
    "/v1/chat/completions",  # Public proxy endpoint (uses API key, not session)
    "/health",               # Health check (public)
    "/metrics",              # Prometheus metrics (public)
}

# Global DDoS protection (application-level rate limit)
GLOBAL_RATE_LIMIT_PER_SECOND = 1000  # 1000 req/sec total across ALL users
GLOBAL_RATE_LIMIT_WINDOW = 1  # 1 second window

# Budget Management Constants
# CRITICAL: Conservative estimate for atomic budget reservation
# This prevents race conditions by reserving max cost before request execution
MAX_ESTIMATED_COST_PER_REQUEST = Decimal("1.00")  # $1.00 max per request (fallback)

def estimate_max_cost_for_model(model: str, num_messages: int = 10) -> Decimal:
    """
    Dynamically estimate maximum cost for a request based on model.
    This is more accurate than fixed $1.00 and reduces over-reservation.
    
    Args:
        model: The OpenAI model name
        num_messages: Estimated number of messages (default: 10)
    
    Returns:
        Conservative maximum cost estimate for this model
    """
    # Estimate ~500 tokens per message (conservative)
    estimated_tokens = num_messages * 500
    
    # Get pricing for model
    base_model = model
    for prefix in PricingEngine.PRICES:
        if model.startswith(prefix):
            base_model = prefix
            break
    
    rates = PricingEngine.PRICES.get(base_model)
    if not rates:
        # Unknown model: use conservative $1.00
        return MAX_ESTIMATED_COST_PER_REQUEST
    
    input_rate, output_rate = rates
    # Assume equal split of tokens (conservative)
    max_cost = (Decimal(estimated_tokens) / Decimal("1000")) * (input_rate + output_rate)
    
    # Add 50% buffer for safety
    max_cost = max_cost * Decimal("1.5")
    
    # Cap at $1.00 to prevent abuse
    return min(max_cost, MAX_ESTIMATED_COST_PER_REQUEST)

# Circuit Breaker Configuration
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5  # Open circuit after 5 failures
CIRCUIT_BREAKER_TIMEOUT = 60  # Seconds before attempting to close circuit
CIRCUIT_BREAKER_MAX_TIMEOUT = 300  # Maximum timeout: 5 minutes

# Enhanced Circuit Breaker State with Exponential Backoff
class CircuitBreakerState:
    def __init__(self):
        self.failures = 0
        self.consecutive_failures = 0
        self.last_failure_time: Optional[float] = None
        self.is_open = False
        self.half_open_attempts = 0
    
    def record_success(self):
        """Reset circuit breaker on success."""
        was_open = self.is_open
        self.failures = 0
        self.consecutive_failures = 0
        self.is_open = False
        self.last_failure_time = None
        self.half_open_attempts = 0
        if was_open:
            log.info("circuit_breaker_closed", message="Circuit breaker recovered after successful request")
    
    def record_failure(self):
        """Record failure and check if circuit should open."""
        self.failures += 1
        self.consecutive_failures += 1
        self.last_failure_time = asyncio.get_event_loop().time()
        if self.failures >= CIRCUIT_BREAKER_FAILURE_THRESHOLD:
            self.is_open = True
            log.warn("circuit_breaker_opened", failures=self.failures, consecutive=self.consecutive_failures)
    
    def get_backoff_timeout(self) -> float:
        """Calculate exponential backoff timeout based on consecutive failures."""
        # Exponential backoff: 60s, 120s, 240s, 300s (max)
        timeout = CIRCUIT_BREAKER_TIMEOUT * (2 ** min(self.consecutive_failures - CIRCUIT_BREAKER_FAILURE_THRESHOLD, 2))
        return min(timeout, CIRCUIT_BREAKER_MAX_TIMEOUT)
    
    def should_attempt(self) -> bool:
        """Check if we should attempt a request (circuit closed or timeout passed)."""
        if not self.is_open:
            return True
        if self.last_failure_time:
            backoff_timeout = self.get_backoff_timeout()
            elapsed = asyncio.get_event_loop().time() - self.last_failure_time
            if elapsed >= backoff_timeout:
                log.info("circuit_breaker_half_open", elapsed=elapsed, backoff_timeout=backoff_timeout)
                self.half_open_attempts += 1
                return True  # Half-open: allow one attempt
        return False

openai_circuit_breaker = CircuitBreakerState()

# ============================================================================
# DEPRECATION: PricingEngine class removed (duplicate)
# ============================================================================
# OPTIMIZATION: PricingEngine is now imported from providers module
# This eliminates code duplication and prevents divergence
# Location: packages/engine/providers/pricing.py
# ============================================================================

# REFACTORING NOTE: CacheData and get_supabase_headers moved to dependencies.py
# Imported above in line 72

# Retry decorator for external API calls
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException)),
    reraise=True
)
async def http_request_with_retry(
    method: str,
    url: str,
    headers: dict,
    json_data: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: float = 30.0
) -> httpx.Response:
    """Make HTTP request with automatic retry on transient failures."""
    response = await core.resources.http_client.request(
        method=method,
        url=url,
        headers=headers,
        json=json_data,
        params=params,
        timeout=timeout
    )
    return response

# ============================================================================
# SUPABASE CLIENT WRAPPER
# ============================================================================
# This wrapper provides a Supabase-like interface for billing.py compatibility
# while using httpx directly for API calls

class SupabaseResponse:
    """Wrapper for Supabase response to match expected interface."""
    def __init__(self, data, count=None):
        self.data = data
        self.count = count

class SupabaseTable:
    """Wrapper for Supabase table operations."""
    def __init__(self, url: str, key: str, client: httpx.AsyncClient, table_name: str):
        self.url = url
        self.key = key
        self.client = client
        self.table_name = table_name
        self._filters = {}
        self._select_fields = "*"
        self._limit_value = None
        self._offset_value = None
        self._single = False
        self._update_data = None
        self._delete = False
        # Range filters (gte/lte/gt/lt) need to coexist with eq() on the same
        # column, so we collect them as a list of ("column", "op.value") pairs
        # in addition to the eq dict.
        self._range_filters: list = []
        self._order = None  # "column.asc" / "column.desc"
        self._count = None  # "exact" asks PostgREST for the total row count

    def select(self, fields: str = "*", count: str = None):
        """Select specific fields; count="exact" also returns the total in .count."""
        self._select_fields = fields
        self._count = count
        return self

    def neq(self, column: str, value):
        """Filter by inequality."""
        self._range_filters.append((column, f"neq.{value}"))
        return self

    def in_(self, column: str, values):
        """Filter where column is one of values."""
        self._range_filters.append((column, "in.(%s)" % ",".join(str(v) for v in values)))
        return self

    def eq(self, column: str, value):
        """Filter by equality."""
        self._filters[column] = f"eq.{value}"
        return self

    def gte(self, column: str, value):
        """Filter where column >= value."""
        self._range_filters.append((column, f"gte.{value}"))
        return self

    def lte(self, column: str, value):
        """Filter where column <= value."""
        self._range_filters.append((column, f"lte.{value}"))
        return self

    def gt(self, column: str, value):
        """Filter where column > value."""
        self._range_filters.append((column, f"gt.{value}"))
        return self

    def lt(self, column: str, value):
        """Filter where column < value."""
        self._range_filters.append((column, f"lt.{value}"))
        return self

    def order(self, column: str, desc: bool = False):
        """Order by column. Pass desc=True for descending."""
        self._order = f"{column}.{'desc' if desc else 'asc'}"
        return self

    def limit(self, count: int):
        """Limit results."""
        self._limit_value = count
        return self

    def or_(self, filters: str):
        """PostgREST or=(...): filters as supabase-py takes them, e.g. "a.eq.1,b.is.null"."""
        self._range_filters.append(("or", "(%s)" % filters))
        return self

    def range(self, start: int, end: int):
        """Rows start..end, inclusive (supabase-py's range): offset and limit."""
        self._offset_value = max(0, int(start))
        self._limit_value = max(0, int(end) - int(start) + 1)
        return self

    def single(self):
        """Expect single result."""
        self._single = True
        return self
    
    async def execute(self):
        """Execute the query."""
        # Handle INSERT operation
        if hasattr(self, '_insert_data') and self._insert_data is not None:
            res = await self.client.post(
                f"{self.url}/rest/v1/{self.table_name}",
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=representation"
                },
                json=self._insert_data
            )
            res.raise_for_status()
            return SupabaseResponse(res.json())
        
        # Handle UPSERT operation
        if hasattr(self, '_upsert_data') and self._upsert_data is not None:
            headers = {
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "Prefer": "return=representation,resolution=merge-duplicates"
            }
            res = await self.client.post(
                f"{self.url}/rest/v1/{self.table_name}",
                headers=headers,
                json=self._upsert_data
            )
            res.raise_for_status()
            return SupabaseResponse(res.json())
        
        # Handle UPDATE operation
        if self._update_data is not None:
            params = list(self._filters.items()) + list(self._range_filters)
            
            res = await self.client.patch(
                f"{self.url}/rest/v1/{self.table_name}",
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=representation"
                },
                params=params,
                json=self._update_data
            )
            res.raise_for_status()
            return SupabaseResponse(res.json())
        
        # Handle DELETE operation
        if self._delete:
            params = list(self._filters.items()) + list(self._range_filters)
            
            res = await self.client.delete(
                f"{self.url}/rest/v1/{self.table_name}",
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}"
                },
                params=params
            )
            res.raise_for_status()
            return SupabaseResponse(res.json())
        
        # Handle SELECT operation. Use a list of tuples so range filters
        # (gte/lte on the same column) survive URL encoding.
        params_list: list = [("select", self._select_fields)]
        for col, val in self._filters.items():
            params_list.append((col, val))
        params_list.extend(self._range_filters)
        if self._order:
            params_list.append(("order", self._order))
        if self._limit_value:
            params_list.append(("limit", str(self._limit_value)))
        if self._offset_value:
            params_list.append(("offset", str(self._offset_value)))

        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        if self._count:
            headers["Prefer"] = f"count={self._count}"
        res = await self.client.get(
            f"{self.url}/rest/v1/{self.table_name}",
            headers=headers,
            params=params_list,
        )
        res.raise_for_status()
        data = res.json()
        count = None
        if self._count:
            total = (res.headers.get("content-range") or "").split("/")[-1]
            count = int(total) if total.isdigit() else len(data or [])

        if self._single and data:
            return SupabaseResponse(data[0] if isinstance(data, list) else data, count)
        return SupabaseResponse(data, count)
    
    def insert(self, data: dict):
        """Insert data into table."""
        self._insert_data = data
        return self
    
    def upsert(self, data: dict, on_conflict: str = None):
        """
        Upsert data into table (insert or update on conflict).
        
        Args:
            data: The data to upsert
            on_conflict: Column name for conflict resolution (e.g., 'stripe_subscription_id')
        
        PostgREST uses 'Prefer: resolution=merge-duplicates' header for upsert.
        The on_conflict column must have a UNIQUE constraint.
        """
        self._upsert_data = data
        self._on_conflict = on_conflict
        return self
    
    def update(self, data: dict):
        """Update data in table (returns self for chaining with eq)."""
        self._update_data = data
        return self
    
    def delete(self):
        """Delete data from table (returns self for chaining with eq)."""
        self._delete = True
        return self

class SupabaseClient:
    """
    Supabase client wrapper for compatibility with billing.py.
    Uses httpx directly to make REST API calls to Supabase.
    """
    def __init__(self, url: str, key: str, client: httpx.AsyncClient):
        self.url = url
        self.key = key
        self.client = client
    
    def table(self, name: str):
        """Get table interface."""
        return SupabaseTable(self.url, self.key, self.client, name)
    
    async def rpc(self, function_name: str, params: dict):
        """Call Supabase RPC function."""
        res = await self.client.post(
            f"{self.url}/rest/v1/rpc/{function_name}",
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json"
            },
            json=params
        )
        res.raise_for_status()
        return SupabaseResponse(res.json())

# Global client variable (initialized in lifespan)
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize shared resources in core module
    log.info("engine_startup", message="Oxy Engine initializing...")
    
    # MEDIUM #3 fix: Add proper timeouts for all external calls
    timeout_config = httpx.Timeout(
        connect=5.0,   # 5 seconds to establish connection
        read=30.0,     # 30 seconds to read response
        write=10.0,    # 10 seconds to write request
        pool=5.0       # 5 seconds to get connection from pool
    )
    
    # Initialize shared resources in ResourceManager
    # PRODUCTION OPTIMIZATION: Use factory method with connection pooling limits
    # This uses object attribute assignment, so all imports see the same object
    core.resources.http_client = core.resources.create_http_client()
    
    # Timeouts: a Redis that stops answering must turn into errors the request path
    # handles (database fallback for keys, refusal for budgets and kill checks),
    # not requests that hang. Healthy Redis answers in milliseconds.
    redis_timeout = float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "1.0"))
    core.resources.redis = aioredis.from_url(
        REDIS_URL, encoding="utf-8", decode_responses=True,
        socket_timeout=redis_timeout, socket_connect_timeout=redis_timeout,
    )
    
    # HIGH #3 fix: Enhanced Redis password validation for production
    if ENVIRONMENT == "production":
        # Test actual connection with AUTH
        try:
            await core.resources.redis.ping()
            log.info("redis_auth_validated", message="Redis password authentication successful")
        except aioredis.AuthenticationError:
            log.critical("fatal_startup_error", error="Redis password incorrect")
            raise EnvironmentError("FATAL: Redis authentication failed")
        except Exception as e:
            log.critical("fatal_startup_error", error=f"Redis connection failed: {e}")
            raise EnvironmentError(f"FATAL: Cannot connect to Redis: {e}")
    else:
        # Development - just check connection
        try:
            await core.resources.redis.ping()
            log.info("engine_startup_complete", message="Clients initialized (HTTPX, Redis).")
        except Exception as e:
            log.critical("redis_connection_failed", error=str(e), exc_info=True)
            raise EnvironmentError(f"FATAL: Could not connect to Redis at {REDIS_URL}")
    
    # Initialize Supabase client wrapper
    core.resources.supabase = SupabaseClient(SUPABASE_URL, SUPABASE_KEY, core.resources.http_client)
    log.info("supabase_client_initialized")
    
    # MEDIUM #2 fix: Initialize cache encryption
    core.resources.cache_encryption = CacheEncryption(SUPABASE_KEY)
    log.info("cache_encryption_initialized", message="Redis cache encryption enabled")
    
    # Initialize Budget Manager for atomic financial operations
    # AUDIT P0 FIX: Budget Manager was created but never integrated
    from core.budget import BudgetManager
    core.resources.budget_manager = BudgetManager(core.resources.redis)
    log.info("budget_manager_initialized", message="Atomic budget operations enabled (Lua scripts auto-loaded on demand)")

    # Initialize deterministic response cache (temperature=0, non-streaming)
    core.resources.response_cache = ResponseCache(core.resources.redis)
    log.info(
        "response_cache_initialized",
        message="Deterministic response caching enabled (non-streaming, temperature=0)",
        ttl_seconds=DETERMINISTIC_RESPONSE_CACHE_TTL,
    )
    
    # MULTI-PROVIDER: Register providers in the global registry
    provider_registry.register(
        "openai",
        OpenAIProvider,
        model_prefixes=["gpt-", "o1-", "text-"]
    )
    provider_registry.register(
        "anthropic",
        AnthropicProvider,
        model_prefixes=["claude-"]
    )
    # Google Gemini provider
    from providers.google_provider import GoogleProvider
    provider_registry.register(
        "google",
        GoogleProvider,
        model_prefixes=["gemini-"]
    )
    log.info("provider_registry_initialized", providers=["openai", "anthropic", "google"])
    
    # Cache Warming
    # Preload hot projects, pricing data, and plan limits into Redis
    # Avoids cold-start lookups on the first requests after a restart
    try:
        from core.cache_warmer import warm_cache_on_startup
        await warm_cache_on_startup(core.resources.http_client)
        log.info("cache_warming_complete", message="Hot data preloaded into Redis")
    except Exception as e:
        # Non-fatal: Continue startup even if cache warming fails
        log_error_safe(log, "cache_warming_failed", e)
        
    yield
    
    await core.resources.http_client.aclose()
    await core.resources.redis.aclose()
    log.info("engine_shutdown_complete", message="Oxy Engine clients closed.")

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Oxy Engine API", 
    version="1.0.5",
    description="""
# Oxy Engine - AI Control Plane

**Kill Switch for Rogue AI Agents**

One API for cloud LLMs (OpenAI, Anthropic, Google) + local models (Ollama, vLLM). Stop rogue agents fast, block data leaks from tools, and control per-agent budgets.

## Features

- 🛑 **Kill Switch**: Stop any rogue agent in milliseconds
- 💰 **Per-Agent Budgets**: Atomic enforcement with Redis Lua scripts
- 🛡️ **Tool/MCP DLP**: Scan tool inputs and outputs for secrets, PII, API keys
- 🌐 **Cloud + Local**: OpenAI, Anthropic, Google + Ollama, vLLM, Docker Model Runner
- 📊 **Agent Dashboard**: Real-time monitoring of all your AI agents
- 🔐 **Enterprise Security**: Row-Level Security (RLS), audit logs, SOC 2 ready

## Authentication

All API endpoints (except auth) require a Bearer token:

```
Authorization: Bearer oxy_live_your_api_key_here
```

Get your API key from the dashboard of your deployment (http://localhost:3000/dashboard on the local stack).

## Rate Limits

- **Proxy Endpoints**: 200 requests/minute per API key
- **Management Endpoints**: 120 requests/minute per user
- **Auth Endpoints**: 30 requests/minute per IP

## Support

- **Documentation**: https://github.com/heyvaldemar/oxy-llm-gateway
- **Issues**: https://github.com/heyvaldemar/oxy-llm-gateway/issues
- **Security**: https://github.com/heyvaldemar/oxy-llm-gateway/security/advisories/new

## OpenAPI Spec

Machine-readable OpenAPI specification available at `/openapi.json` for AI agents and API clients.

---

## 🤖 AI Agent Integration Guide

**For autonomous AI agents (AutoGPT, Claude MCP, LangChain, etc.):**

### Quick Start for AI Agents
1. Obtain API key from dashboard or `/v1/auth/login` endpoint
2. Set header: `Authorization: Bearer oxy_live_...`
3. For agent sessions, include: `X-Oxy-Agent-Id`, `X-Oxy-Session-Id`
4. Call `/v1/chat/completions` with OpenAI-compatible format

### Key Endpoints for Agents
- `POST /v1/chat/completions` - Send LLM requests (OpenAI format)
- `POST /v1/agents/{agent_id}/sessions` - Start a tracked session
- `DELETE /v1/agents/{agent_id}/sessions/{session_id}` - Kill switch
- `GET /v1/agents/{agent_id}/budget` - Check remaining budget

### Error Codes
- `401`: Invalid/expired token - re-authenticate
- `402`: Budget exceeded - request budget increase or wait for reset
- `410`: Session killed - create new session
- `429`: Rate limited - wait and retry with exponential backoff
- `451`: Content blocked by DLP - remove sensitive data
- `503`: Provider unavailable - retry with different model

### Best Practices for AI Agents
- Always check budget before expensive operations
- Use session tracking for audit trail
- Handle 410 (Gone) gracefully - session may have been killed
- Implement exponential backoff for 429/503 errors
    """,
    summary="AI Control Plane with Kill Switch for Agents & Tool DLP",
    contact={
        "name": "Oxy contributors",
        "url": "https://github.com/heyvaldemar/oxy-llm-gateway",
    },
    license_info={
        "name": "Apache-2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0",
    },
    servers=[
        {
            "url": "http://localhost:8000",
            "description": "Local development server"
        }
    ],
    # OpenAPI tags with machine-readable hints for LLM agents
    openapi_tags=[
        {
            "name": "proxy",
            "description": "LLM Proxy - Send requests to any AI model (OpenAI, Anthropic, Google, Ollama). Use POST /v1/chat/completions for chat, POST /v1/embeddings for vectors.",
            "x-llm-hint": "Primary endpoint for AI inference. Include X-Oxy-Session-Id header for session tracking."
        },
        {
            "name": "agents",
            "description": "Agent Management - Create, configure, and control AI agents. Use kill switch to stop rogue agents immediately.",
            "x-llm-hint": "Use DELETE /v1/agents/{id}/sessions/{sid} to kill a session. Check GET /v1/agents/{id}/budget before expensive operations."
        },
        {
            "name": "billing",
            "description": "Billing (optional Stripe module) - Stripe subscriptions, invoices and plan changes. Answers 503 when Stripe is not configured.",
            "x-llm-hint": "Human intervention usually required. Do not automate billing changes."
        },
        {
            "name": "auth",
            "description": "Authentication - Login, token refresh, API key management. Required for all other endpoints.",
            "x-llm-hint": "Call POST /v1/auth/login first to get JWT. For API key auth, use Authorization: Bearer oxy_live_...."
        },
        {
            "name": "mcp",
            "description": "Model Context Protocol - Register and manage MCP servers/tools for agent capabilities.",
            "x-llm-hint": "Register tools here so agents can use them. DLP automatically scans tool inputs/outputs."
        },
        {
            "name": "prompts",
            "description": "Prompt Templates - Store and version prompt templates for consistent agent behavior.",
            "x-llm-hint": "Use templates for reproducible prompts. Variables use {{name}} syntax."
        }
    ],
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
    redoc_url=None
)

# Add rate limiter to app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ============================================================================
# SECURITY: CSRF VALIDATION MIDDLEWARE
# ============================================================================
# ============================================================================
# MIDDLEWARE INTEGRATION
# ============================================================================
# Middleware has been refactored into packages/engine/middleware/
# This ensures:
# - Single Responsibility Principle
# - Easy testing and modification
# - Clear execution order
# - No code duplication
#
# Order matters! Applied bottom-to-top:
# 1. Security headers (first)
# 2. Request processing (ID, size limits)
# 3. Rate limiting (auth, global)
# 4. CSRF protection (last)
# 5. Geo-blocking (optional, enable via ENABLE_GEO_BLOCKING=true)
# ============================================================================

# Apply middleware in correct order (bottom-to-top execution)
app.middleware("http")(csrf_protection_middleware)
app.middleware("http")(global_rate_limit_middleware)
app.middleware("http")(auth_rate_limit_middleware)
app.middleware("http")(add_rate_limit_headers_middleware)
app.middleware("http")(limit_request_size)
app.middleware("http")(add_request_id_middleware)
app.middleware("http")(add_security_headers)

# CRITICAL: API Key Rotation Enforcement (SOC 2 Compliance)
app.middleware("http")(key_rotation_middleware)
log.info(
    "key_rotation_policy_configured",
    message="API key rotation middleware configured (KEY_ROTATION_MODE env; default warn-only)",
    compliance="SOC 2 Ready (controls only; certification depends on deployment)",
)

# Optional: Geo-blocking middleware (enable via environment variable)
if os.getenv("ENABLE_GEO_BLOCKING", "false").lower() == "true":
    app.middleware("http")(geo_blocking_middleware)
    log.info("geo_blocking_enabled", message="Geo-blocking middleware activated")

# Include billing router (Stripe subscription management)
from routes.billing import router as billing_router
app.include_router(billing_router)

# Billing overview (plan/usage/referrals)
from routes.billing_overview import router as billing_overview_router
app.include_router(billing_overview_router)

# Exports (CSV/Audit)
from routes.exports import router as exports_router
app.include_router(exports_router)

# Attribution summary (cost tags)
from routes.attribution_summary import router as attribution_summary_router
app.include_router(attribution_summary_router)

# Prompt library (org-scoped + verified)
from routes.prompts_v1 import router as prompts_v1_router
app.include_router(prompts_v1_router)

# Cost optimizer (recommendations)
from routes.cost_optimizer_v1 import router as cost_optimizer_v1_router
app.include_router(cost_optimizer_v1_router)

# Privacy & retention controls
from routes.privacy import router as privacy_router
app.include_router(privacy_router)

# Audit logs
from routes.audit import router as audit_router
app.include_router(audit_router)

# Setup status (5-minute setup)
from routes.setup_v1 import router as setup_v1_router
app.include_router(setup_v1_router)

# API Keys management (dashboard)
from routes.api_keys_v1 import router as api_keys_v1_router
app.include_router(api_keys_v1_router)

# Team management
from routes.team_v1 import router as team_v1_router
app.include_router(team_v1_router)

# Agent sessions
from routes.agent_sessions_v1 import router as agent_sessions_v1_router
app.include_router(agent_sessions_v1_router)

# Notifications & webhooks
from routes.notifications_v1 import router as notifications_v1_router
app.include_router(notifications_v1_router)

# Organization settings
from routes.org_v1 import router as org_v1_router
app.include_router(org_v1_router)

# Provider settings
from routes.providers_v1 import router as providers_v1_router
app.include_router(providers_v1_router)

# Include credits router (referral credits)
from routes.credits import router as credits_router
app.include_router(credits_router)

# Include proxy router (AI Gateway core - REFACTORED for modularity)
from routes.proxy import router as proxy_router
app.include_router(proxy_router)
log.info("proxy_router_loaded",
         message="Proxy endpoint extracted to routes/proxy.py",
         maintainability="Reduced main.py by 572 lines")

# Include SSO router (SAML/OIDC single sign-on)
from routes.sso import router as sso_router
app.include_router(sso_router)

# Team management
app.include_router(team_router)
log.info("sso_router_loaded",
         message="Enterprise SSO/SAML enabled")

# Include reports router (CSV export)
# CSV export of the request log: GET /v1/exports/requests.csv (routes/exports.py).

# REFACTORING PHASE 1: Include refactored auth router
# Handles: /management/onboard, /management/profile, /management/provider
app.include_router(auth_router)

# Include add-ons router (per-organization feature flags)
app.include_router(add_ons_router)
log.info("add_ons_router_loaded",
         message="Add-ons router active (SSO, white-label, etc.)")

# Include cost optimizer router (cost recommendations)
app.include_router(cost_optimizer_router)
log.info("cost_optimizer_router_loaded",
         message="Cost optimizer router active")

# Include prompts router (prompt library)
app.include_router(prompts_router)
log.info("prompts_router_loaded",
         message="Prompt library router active")

# Include intelligence router (budget forecast + insights)
app.include_router(intelligence_router)
log.info("intelligence_router_loaded",
         message="Budget forecast + intelligence router active")

# Include guardrails router (Kill switch / auto-suspend control)
from routes.guardrails import router as guardrails_router
app.include_router(guardrails_router)
log.info("guardrails_router_loaded",
         message="Guardrails API active")

# Include requests router (Request activity - visibility)
from routes.requests import router as requests_router
app.include_router(requests_router)
log.info("requests_router_loaded",
         message="Request activity endpoint active")

# ============================================================================
# 🆕 AGENT SUPPORT (AI Control Plane)
# ============================================================================

# Include MCP router (Model Context Protocol - TOOL ECOSYSTEM)
from routes.mcp import router as mcp_router
app.include_router(mcp_router)
log.info("mcp_router_loaded",
         message="MCP (Model Context Protocol) integration active")

# Include agents router (Agent Management - CONTROL PLANE)
from routes.agents import router as agents_router
app.include_router(agents_router)
log.info("agents_router_loaded",
         message="Agent Management API active",
         endpoint="/v1/agents")

# Include public router (Marketing metrics - NO AUTH REQUIRED)
from routes.public import router as public_router
app.include_router(public_router)
log.info("public_router_loaded",
         message="Public metrics API active (landing page)")

# Security: Trusted Host Middleware
# CRITICAL: ALLOWED_HOSTS must include Docker service names for internal networking
# FIX: Always include Docker service names regardless of env variable
# This prevents 400 "Invalid host header" errors when UI calls engine internally
allowed_hosts = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
# Strip whitespace and filter empty strings
allowed_hosts = [h.strip() for h in allowed_hosts if h.strip()]
# ALWAYS add Docker service names for inter-container communication
# These are required for docker-compose internal networking
DOCKER_SERVICE_HOSTS = ["engine", "localhost", "127.0.0.1"]
for docker_host in DOCKER_SERVICE_HOSTS:
    if docker_host not in allowed_hosts:
        allowed_hosts.append(docker_host)
if "*" not in allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    log.info("trusted_host_middleware_configured", allowed_hosts=allowed_hosts)

# CORS Configuration - More restrictive
origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "X-CSRF-Token"],
    expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
    max_age=3600,
)

# NOTE: OLD INLINE MIDDLEWARE REMOVED
# All middleware is now imported from packages/engine/middleware/
# This eliminates 350+ lines of inline code and improves maintainability

# DEPRECATED: Old inline middleware definitions (REMOVED)
# All middleware has been refactored to packages/engine/middleware/
# - csrf_protection_middleware → middleware/security.py
# - add_request_id_middleware → middleware/request.py
# - add_rate_limit_headers_middleware → middleware/rate_limit.py
# - add_security_headers → middleware/security.py
# - limit_request_size → middleware/request.py
# - auth_rate_limit_middleware → middleware/rate_limit.py
# - global_rate_limit_middleware → middleware/rate_limit.py
# - geo_blocking_middleware → middleware/geo_blocking.py

# NOTE: All middleware is now managed via middleware/ module
# No inline middleware definitions remain in this file

# REFACTORING NOTE: hash_key() moved to dependencies.py (imported above)
def hash_key(key: str) -> str:
    """DEPRECATED: Use hash_key from dependencies.py instead."""
    from dependencies import hash_key as _hash_key
    return _hash_key(key)

def generate_oxy_key() -> tuple[str, str]: 
    prefix = secrets.token_hex(KEY_PREFIX_BYTES)
    suffix = secrets.token_hex(KEY_SUFFIX_BYTES)
    full_key = f"oxy_live_{prefix}{suffix}"
    key_prefix = f"oxy_live_{prefix}"
    return full_key, key_prefix

# REFACTORING NOTE: get_user_and_key_from_api_key moved to dependencies.py (imported above)
# Original function (158 lines) handled API key authentication with Redis caching and Supabase fallback
# Now imported from dependencies module for reuse across routes

# REFACTORING NOTE: get_current_user_id moved to dependencies.py (imported above)  
# Original function handled JWT validation with defense-in-depth approach
# Now imported from dependencies module for reuse across routes

# Export verify_jwt for billing.py compatibility
# Set in core module to avoid circular import
# NOTE: Using imported get_current_user_id from dependencies.py
core.verify_jwt = Depends(get_current_user_id)

# ============================================================================
# ORGANIZATION & RBAC HELPERS
# ============================================================================

async def get_user_organizations(user_id: str) -> list[dict]:
    """
    Get all organizations where user is an active member.
    Returns list of organization records with role information.
    """
    try:
        org_url = f"{SUPABASE_URL}/rest/v1/organization_members"
        org_params = {
            "select": "organization_id,role,organizations(id,name,owner_user_id)",
            "user_id": f"eq.{user_id}",
            "status": "eq.active"
        }
        
        # 🔎 DEBUGGER: Log organization membership query (DEVELOPMENT ONLY)
        if ENVIRONMENT == "development":
            log.info(
                "get_user_organizations_query",
                user_id=user_id,
                query_url=org_url,
                query_params=org_params
            )
        
        res = await core.resources.http_client.get(
            org_url,
            params=org_params,
            headers=get_supabase_headers(service_role=True),
            timeout=10.0
        )
        
        # 🔎 DEBUGGER: Log response (DEVELOPMENT ONLY)
        if ENVIRONMENT == "development":
            log.info(
                "get_user_organizations_response",
                user_id=user_id,
                status_code=res.status_code,
                has_content=bool(res.content)
            )
        
        res.raise_for_status()
        data = res.json()
        
        # 🔎 DEBUGGER: Log parsed data (DEVELOPMENT ONLY)
        if ENVIRONMENT == "development":
            log.info(
                "get_user_organizations_data",
                user_id=user_id,
                member_count=len(data),
                raw_data=data if len(data) <= 3 else data[:3]  # Limit to first 3 for readability
            )
        
        result = [
            {
                "organization_id": member["organization_id"],
                "role": member["role"],
                **member["organizations"]
            }
            for member in data
        ]
        
        return result
    except httpx.HTTPStatusError as e:
        # 🔎 DEBUGGER: HTTP error (ALWAYS LOG ERRORS)
        error_body = e.response.text if e.response else "No response body"
        log.error(
            "get_user_organizations_http_error",
            user_id=user_id,
            status_code=e.response.status_code if e.response else "unknown",
            error_body=error_body if ENVIRONMENT == "development" else "[REDACTED]",  # Hide error body in production
            exc_info=False  # Don't log stack trace in production (security)
        )
        return []
    except Exception as e:
        # 🔎 DEBUGGER: Unexpected error (ALWAYS LOG ERRORS)
        log.error(
            "get_user_organizations_unexpected_error",
            user_id=user_id,
            error_type=type(e).__name__,
            error_message=str(e) if ENVIRONMENT == "development" else "[REDACTED]",  # Hide error message in production
            exc_info=False  # Don't log stack trace in production (security)
        )
        return []

async def check_organization_permission(
    user_id: str, 
    organization_id: str, 
    required_role: str = "developer"
) -> bool:
    """
    Check if user has required role in organization.
    Uses fn_check_org_permission RPC function.
    """
    try:
        res = await core.resources.http_client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/fn_check_org_permission",
            headers=get_supabase_headers(),
            json={
                "p_user_id": user_id,
                "p_organization_id": organization_id,
                "p_required_role": required_role
            }
        )
        res.raise_for_status()
        return res.json()  # Returns boolean
    except Exception as e:
        # Use log_error_safe
        log_error_safe(log, "check_organization_permission_failed", e,
                      user_id=user_id, 
                      organization_id=organization_id)
        return False

async def log_request(
    user_id: str,
    project_id: str,
    organization_id: str,  # HIGH #2 fix: Add organization_id for audit logs
    model: str,
    provider: str,  # UNIVERSAL ROUTER: Track which provider was used
    status: str, 
    threat: bool, 
    threat_type: str | None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: Decimal = Decimal("0.0"),
    subscription_id: str | None = None,
    reserved_amount: Decimal = MAX_ESTIMATED_COST_PER_REQUEST
):
    """
    Log request and atomically update budget + subscription usage.
    
    Uses BudgetManager for atomic budget adjustment.
    This prevents race conditions when multiple requests complete simultaneously.
    
    HIGH #2 fix: Includes organization_id for SOC 2 compliance audit trail.
    
    Note: Budget was pre-reserved with reserved_amount in proxy().
    We now adjust it to the actual cost atomically.
    """
    try:
        # Use BudgetManager for atomic budget adjustment with floor check
        # Adjustment = Actual Cost - Reserved Amount
        # If cost < reserved, adjustment is negative (refund difference)
        # If cost > reserved, adjustment is positive (charge difference)
        adjustment = cost - reserved_amount
        
        try:
            result = await core.resources.budget_manager.adjust_budget(
                project_id=project_id,
                adjustment=adjustment
            )
            
            if result["status"] == "FLOOR_HIT":
                # Budget went negative - this is a bug, log warning
                log.warn("budget_adjustment_floor_hit",
                        project_id=project_id,
                        organization_id=organization_id,
                        old_budget=str(result.get("old_value", "N/A")),
                        adjustment=str(adjustment),
                        new_budget=str(result.get("current_spend", "N/A")))
        except Exception as e:
            log.error("budget_adjustment_failed_manager", project_id=project_id, error=str(e))
            # Fallback to non-atomic (better than nothing)
            budget_key = f"project_spend:{project_id}"
            await core.resources.redis.incrbyfloat(budget_key, float(adjustment))
        
        # Update subscription usage with actual tokens
        if subscription_id:
            try:
                total_tokens = prompt_tokens + completion_tokens
                await core.resources.supabase.rpc("fn_increment_usage", {
                    "p_subscription_id": subscription_id,
                    "p_requests_delta": 1,
                    "p_tokens_delta": total_tokens
                }).execute()
            except Exception as e:
                # Log error but don't fail the request
                log_error_safe(log, "subscription_usage_update_failed", e, subscription_id=subscription_id)
        
        log_payload = {
            "user_id": user_id, 
            "project_id": project_id,
            "organization_id": organization_id,  # HIGH #2 fix: Include for audit trail
            "model": model,
            "provider": provider,  # UNIVERSAL ROUTER: Track provider (openai, anthropic, google)
            "status": status,
            "threat_detected": threat, 
            "threat_type": threat_type,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost": str(cost),
            "created_at": datetime.now(timezone.utc).isoformat() 
        }
        
        await core.resources.redis.lpush("log_queue:request_logs", json.dumps(log_payload))
        
    except Exception as e:
        # Use log_error_safe
        log_error_safe(log, "log_queuing_failed", e, user_id=user_id, project_id=project_id)

# REFACTORING NOTE: validate_openai_key() and validate_anthropic_key() duplicated in routes/auth.py
# These versions in main.py are used by management endpoints still in this file.
# TODO PHASE 2: Extract remaining management endpoints, then remove these duplicates.

async def validate_openai_key(key: str) -> bool:
    """ZOMBIE FUNCTION: Duplicate exists in routes/auth.py. This is used by legacy endpoints in main.py."""
    if not (key.startswith("sk-") or key.startswith("sk-proj-")):
        log.warn("key_validation_failed", reason="Invalid key format", provider="openai")
        return False
    try:
        headers = {"Authorization": f"Bearer {key}"}
        response = await core.resources.http_client.get("https://api.openai.com/v1/models", headers=headers, timeout=10.0)
        if response.status_code == 200:
            log.info("key_validation_success", key_prefix=f"{key[:8]}...", provider="openai")
            return True
        elif response.status_code == 401:
            log.warn("key_validation_failed", reason="401 Unauthorized", key_prefix=f"{key[:8]}...", provider="openai")
            return False
        else:
            log.error("openai_api_error_on_validation", status=response.status_code)
            return False
    except httpx.ConnectError as e:
        log.error("key_validation_connection_failed", error=str(e), provider="openai")
        raise HTTPException(status_code=504, detail="Failed to connect to OpenAI. Check network connection.")
    except httpx.TimeoutException as e:
        log.error("key_validation_timeout", error=str(e), provider="openai")
        raise HTTPException(status_code=504, detail="OpenAI API request timed out. Please try again.")
    except Exception as e:
        log.error("key_validation_unknown_error", error=str(e), exc_info=True, provider="openai")
        return False

async def validate_anthropic_key(key: str) -> bool:
    """ZOMBIE FUNCTION: Duplicate exists in routes/auth.py. This is used by legacy endpoints in main.py."""
    if not key.startswith("sk-ant-"):
        log.warn("key_validation_failed", reason="Invalid key format", provider="anthropic")
        return False
    try:
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        payload = {"model": "claude-3-haiku-20240307", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 1}
        response = await core.resources.http_client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=10.0)
        if response.status_code == 200:
            log.info("key_validation_success", key_prefix=f"{key[:10]}...", provider="anthropic")
            return True
        elif response.status_code == 401:
            log.warn("key_validation_failed", reason="401 Unauthorized", key_prefix=f"{key[:10]}...", provider="anthropic")
            return False
        else:
            log.error("anthropic_api_error_on_validation", status=response.status_code)
            return False
    except httpx.ConnectError as e:
        log.error("key_validation_connection_failed", error=str(e), provider="anthropic")
        raise HTTPException(status_code=504, detail="Failed to connect to Anthropic. Check network connection.")
    except httpx.TimeoutException as e:
        log.error("key_validation_timeout", error=str(e), provider="anthropic")
        raise HTTPException(status_code=504, detail="Anthropic API request timed out. Please try again.")
    except Exception as e:
        log.error("key_validation_unknown_error", error=str(e), exc_info=True, provider="anthropic")
        return False

async def clear_project_cache(project_id: str):
    """Drop the cached records of every key of a project (after a budget or project change).

    The project's spend counter (project_spend:{id}) is the budget ledger and is
    never touched here: deleting it used to hand the project its whole budget again.
    """
    project_keys_set = project_keys_set_name(project_id)
    try:
        # The set holds SHA-256 hashes of the keys, never the keys themselves.
        project_key_hashes = await core.resources.redis.smembers(project_keys_set)
        async with core.resources.redis.pipeline(transaction=True) as pipe:
            for key_hash in project_key_hashes:
                pipe.delete(api_key_cache_name_for_hash(key_hash))
            pipe.delete(project_keys_set)
            await pipe.execute()
        log.info("project_cache_invalidated", project_id=project_id, keys_cleared=len(project_key_hashes))
    except Exception as e:
        log.error("cache_invalidation_failed", project_id=project_id, exc_info=True)

# REFACTORING NOTE: clear_user_cache() duplicated in routes/auth.py
# This version in main.py is used by management endpoints still in this file.
# TODO PHASE 2: Extract remaining management endpoints, then remove this duplicate.

async def clear_user_cache(user_id: str):
    """ZOMBIE FUNCTION: Duplicate exists in routes/auth.py. Used by legacy endpoints in main.py."""
    try:
        res = await core.resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/projects?select=id&user_id=eq.{user_id}&status=neq.deleted",
            headers=get_supabase_headers()
        )
        res.raise_for_status()
        projects = res.json()
        for project in projects:
            await clear_project_cache(project['id'])
    except Exception as e:
        log.error("clear_user_cache_failed", user_id=user_id, exc_info=True)

async def deactivate_dead_key(user_id: str, project_id: str, api_key: str):
    """
    Deactivate a dead OpenAI key that returned 401 Unauthorized.
    
    CRITICAL: This prevents wasted API calls and provides clear UX
    by marking the provider key as invalid in the database.
    """
    lock_name = f"lock:deactivate:{hash_key(api_key)}"
    if not await core.resources.redis.set(lock_name, "1", ex=60, nx=True):
        log.warn("deactivation_skipped", user_id=user_id, reason="already_in_progress")
        return
    
    log.warn("dead_key_detected", user_id=user_id, project_id=project_id, action="Deactivating provider key in database")
    
    try:
        # STEP 1: Deactivate provider key in database (don't delete, mark as invalid)
        # This preserves audit trail and allows user to see why their key failed
        res = await core.resources.http_client.patch(
            f"{SUPABASE_URL}/rest/v1/providers",
            params={
                "user_id": f"eq.{user_id}",
                "provider_name": f"eq.{OPENAI_PROVIDER_NAME}"
            },
            headers=get_supabase_headers(prefer="return=minimal"),
            json={
                "is_active": False,
                "deactivated_reason": "OpenAI returned 401 Unauthorized - key is invalid or revoked",
                "deactivated_at": datetime.now(timezone.utc).isoformat()
            }
        )
        res.raise_for_status()
        
        # STEP 2: Clear ALL caches related to this user/project
        # This ensures immediate deactivation across all API keys
        await clear_project_cache(project_id)
        
        log.info("dead_key_deactivated", user_id=user_id, project_id=project_id, provider="openai")

        log.info("cache_cleared_granular", reason="dead_key_deactivated", api_key_prefix=api_key[:KEY_PREFIX_LENGTH])
    except Exception as e:
        log.error("failed_to_delete_dead_key", user_id=user_id, exc_info=True)

def sanitize_input(v: str) -> str:
    if v:
        return html.escape(v).strip()
    return v

# REFACTORING NOTE: OnboardRequest, ProfileUpdateRequest, ProviderUpdateRequest
# moved to routes/auth.py. These classes are UNUSED ZOMBIES left after refactoring.
# Keeping as placeholders to prevent import errors in legacy code.

# class OnboardRequest(BaseModel): [MOVED TO routes/auth.py]
# class ProfileUpdateRequest(BaseModel): [MOVED TO routes/auth.py]  
# class ProviderUpdateRequest(BaseModel): [MOVED TO routes/auth.py]

class ProjectCreateRequest(BaseModel):
    """Request model for creating a new project."""
    name: str = Field(..., min_length=1, max_length=100, description="Project name (1-100 characters)")

    @field_validator('name')
    def sanitize_name(cls, v):
        return sanitize_input(v)

class ProjectUpdateRequest(BaseModel):
    """Request model for updating an existing project."""
    name: str = Field(..., min_length=1, max_length=100, description="Updated project name (1-100 characters)")
    budget_limit: condecimal(ge=Decimal('0.0'), max_digits=18, decimal_places=2) = Field(
        Decimal('0.0'),
        description="Total spend cap for the project in USD (e.g., 150.00); nothing resets it. Set to 0 for no cap."
    )

    @field_validator('name')
    def sanitize_name(cls, v):
        return sanitize_input(v)

# Global exception handler to prevent information leakage
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler to prevent information leakage in production."""
    log.error("unhandled_exception", path=request.url.path, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "type": "internal_error"}
    )

# REFACTORING NOTE: The following 5 management endpoints moved to routes/auth.py:
# - POST /management/onboard (onboarding logic)
# - DELETE /management/onboard (offboarding logic)
# - POST /management/profile (update profile)
# - POST /management/provider (update provider keys)
# - DELETE /management/provider (disconnect provider)
# These are now registered via app.include_router(auth_router) above

# ============================================================================
# MANAGEMENT ENDPOINTS REMAINING IN MAIN.PY
# ============================================================================
# NOTE: Auth-related management endpoints moved to routes/auth.py
# The following endpoints remain here for now:

@app.get("/management/status")
@limiter.limit(f"{RATE_LIMIT_MANAGEMENT_PER_MINUTE}/minute")
async def management_status(request: Request, user_id: str = Depends(get_current_user_id)):
    """
    Check user onboarding status and organization membership.
    
    Returns:
    - onboarded: Has profile and organization
    - provider_connected: Organization has active OpenAI provider
    - organization_id: User's primary organization ID
    - organization_name: Organization name
    - role: User's role in organization
    """
    try:
        # 🔎 DEBUGGER: Log status check initiation
        log.info(
            "management_status_check_start",
            user_id=user_id,
            endpoint="/management/status"
        )
        
        # Check if profile exists
        profile_url = f"{SUPABASE_URL}/rest/v1/profiles?select=id&id=eq.{user_id}&limit=1"
        
        # 🔎 DEBUGGER: Log profile query
        log.info(
            "management_status_profile_query",
            user_id=user_id,
            query_url=profile_url,
            query_type="profile_existence"
        )
        
        res_profile = await core.resources.http_client.get(
            profile_url,
            headers=get_supabase_headers(prefer="count=exact", service_role=True),
            timeout=10.0
        )
        
        # 🔎 DEBUGGER: Log profile response
        log.info(
            "management_status_profile_response",
            user_id=user_id,
            status_code=res_profile.status_code,
            has_content=bool(res_profile.content),
            content_range_header=res_profile.headers.get("content-range", "missing")
        )
        
        res_profile.raise_for_status()
        profile_count = int(res_profile.headers.get("content-range", "0/0").split("/")[-1] or 0)
        
        if profile_count == 0:
            # 🔎 DEBUGGER: Profile not found
            log.warning(
                "management_status_profile_not_found",
                user_id=user_id,
                message="Profile does not exist, user needs to complete onboarding"
            )
            return {
                "onboarded": False,
                "provider_connected": False,
                "organization_id": None,
                "organization_name": None,
                "role": None
            }
        
        # 🔎 DEBUGGER: Profile found
        log.info(
            "management_status_profile_found",
            user_id=user_id,
            profile_count=profile_count
        )
        
        # Get user's organizations
        # 🔎 DEBUGGER: Log organization query
        log.info(
            "management_status_org_query_start",
            user_id=user_id,
            query_type="organization_membership"
        )
        
        user_orgs = await get_user_organizations(user_id)
        
        # 🔎 DEBUGGER: Log organization results
        log.info(
            "management_status_org_query_result",
            user_id=user_id,
            org_count=len(user_orgs),
            org_ids=[org.get("organization_id") for org in user_orgs] if user_orgs else []
        )
        
        if not user_orgs:
            # Profile exists but no organization (shouldn't happen with new onboarding)
            # 🔎 DEBUGGER: Organization not found
            log.warning(
                "management_status_org_not_found",
                user_id=user_id,
                message="Profile exists but no organization membership found"
            )
            return {
                "onboarded": False,
                "provider_connected": False,
                "organization_id": None,
                "organization_name": None,
                "role": None
            }
        
        # Use first organization as primary (in future, add user preference)
        primary_org = user_orgs[0]
        org_id = primary_org["organization_id"]
        
        # 🔎 DEBUGGER: Organization found
        log.info(
            "management_status_org_found",
            user_id=user_id,
            organization_id=org_id,
            organization_name=primary_org.get("name"),
            role=primary_org.get("role")
        )
        
        # Check if organization has active provider
        provider_url = f"{SUPABASE_URL}/rest/v1/providers"
        provider_params = {
            "select": "id,is_active",
            "organization_id": f"eq.{org_id}",
            "provider_name": f"eq.{OPENAI_PROVIDER_NAME}",
            "limit": "1"
        }
        
        # 🔎 DEBUGGER: Log provider query
        log.info(
            "management_status_provider_query",
            user_id=user_id,
            organization_id=org_id,
            query_url=provider_url,
            query_params=provider_params,
            query_type="provider_check"
        )
        
        res_provider = await core.resources.http_client.get(
            provider_url,
            params=provider_params,
            headers=get_supabase_headers(service_role=True),
            timeout=10.0
        )
        
        # 🔎 DEBUGGER: Log provider response
        log.info(
            "management_status_provider_response",
            user_id=user_id,
            organization_id=org_id,
            status_code=res_provider.status_code,
            has_content=bool(res_provider.content)
        )
        
        res_provider.raise_for_status()
        providers = res_provider.json()
        
        # 🔎 DEBUGGER: Log provider data
        log.info(
            "management_status_provider_data",
            user_id=user_id,
            organization_id=org_id,
            provider_count=len(providers),
            providers_data=providers
        )
        
        # Provider must exist AND be active
        provider_connected = len(providers) > 0 and providers[0].get("is_active", False)
        
        # 🔎 DEBUGGER: Final status determination
        log.info(
            "management_status_check_complete",
            user_id=user_id,
            organization_id=org_id,
            onboarded=True,
            provider_connected=provider_connected,
            final_status="success"
        )
        
        return {
            "onboarded": True,
            "provider_connected": provider_connected,
            "organization_id": org_id,
            "organization_name": primary_org.get("name"),
            "role": primary_org.get("role")
        }
    except httpx.HTTPStatusError as e:
        # 🔎 DEBUGGER: HTTP error from Supabase (400, 404, 500, etc.)
        error_body = e.response.text if e.response else "No response body"
        log.error(
            "management_status_http_error",
            user_id=user_id,
            status_code=e.response.status_code if e.response else "unknown",
            error_body=error_body,
            request_url=str(e.request.url) if e.request else "unknown",
            exc_info=True
        )
        
        # Expose detailed error to help debug
        raise HTTPException(
            status_code=e.response.status_code if e.response else 500,
            detail=f"Database query failed: {error_body[:500]}"  # Limit error message length
        )
    except Exception as e:
        # 🔎 DEBUGGER: Unexpected error
        error_message = str(e)
        log.error(
            "management_status_unexpected_error",
            user_id=user_id,
            error_type=type(e).__name__,
            error_message=error_message,
            exc_info=True
        )
        
        # Expose error type to help debug
        raise HTTPException(
            status_code=500,
            detail=f"Status check failed: {type(e).__name__}: {error_message[:200]}"
        )

@app.get("/management/projects")
@limiter.limit(f"{RATE_LIMIT_MANAGEMENT_PER_MINUTE}/minute")
async def management_list_projects(request: Request, user_id: str = Depends(get_current_user_id)):
    """
    List all projects across user's organizations.
    Returns projects with organization name included.
    """
    try:
        # Get user's organizations
        user_orgs = await get_user_organizations(user_id)
        
        if not user_orgs:
            return []
        
        # Get organization IDs
        org_ids = [org["organization_id"] for org in user_orgs]
        
        # Query projects in user's organizations
        res = await core.resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/projects",
            params={
                "select": "id,name,budget_limit,current_spend,organization_id,created_at,organizations(name)",
                "organization_id": f"in.({','.join(org_ids)})",
                "status": "neq.deleted",
                "order": "created_at.desc"
            },
            headers=get_supabase_headers()
        )
        res.raise_for_status()
        projects = res.json()
        
        # Flatten organization name into project object
        for project in projects:
            if project.get("organizations"):
                project["organization_name"] = project["organizations"].get("name")
                del project["organizations"]
        
        return projects
    except Exception as e:
        log.error("project_list_failed", user_id=user_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list projects.")

@app.post("/management/project")
@limiter.limit(f"{RATE_LIMIT_MANAGEMENT_PER_MINUTE}/minute")
async def management_create_project(request: Request, req: ProjectCreateRequest, user_id: str = Depends(get_current_user_id)):
    """
    Create a new project in an organization.
    Requires admin or owner role in the organization.
    
    SECURITY: Project limits enforced by subscription tier to prevent database spam.
    """
    try:
        # TEMPORARY: Get user's first organization until frontend sends organization_id
        user_orgs = await get_user_organizations(user_id)
        
        if not user_orgs:
            raise HTTPException(status_code=403, detail="You are not a member of any organization")
        
        organization_id = user_orgs[0]["organization_id"]
        
        # Check if user has admin or owner role
        has_permission = await check_organization_permission(
            user_id,
            organization_id,
            required_role="admin"
        )
        
        if not has_permission:
            raise HTTPException(
                status_code=403,
                detail="Only admins and owners can create projects"
            )
        
        # ========================================================================
        # FRAUD PREVENTION #1: RATE LIMITING (5 PROJECTS/HOUR)
        # ========================================================================
        # PURPOSE: Prevent rapid-fire project spam attacks
        # 
        # ATTACK VECTOR:
        #   - Attacker writes script to POST /management/project in a loop
        #   - Creates 1,000 projects before tier limit check can stop them
        #   - Result: Database bloat, storage costs spike
        # 
        # MITIGATION:
        #   - Redis rate limit: Max 5 project creations per hour per user
        #   - Independent of tier limits (additional security layer)
        #   - Expires after 1 hour (TTL)
        # 
        # ENFORCEMENT: Redis atomic counter (cannot be bypassed)

        # ========================================================================
        
        rate_limit_key = f"project_creation_rate:{user_id}"
        
        try:
            # Increment counter atomically
            count = await resources.redis.incr(rate_limit_key)
            
            # Set TTL on first increment (1 hour = 3600 seconds)
            if count == 1:
                await resources.redis.expire(rate_limit_key, 3600)
            
            # Check rate limit (5 projects per hour)
            if count > 5:
                log.warning("project_creation_rate_limit_exceeded",
                           user_id=user_id,
                           organization_id=organization_id,
                           count=count,
                           limit=5,
                           severity="FRAUD_PREVENTION")
                raise HTTPException(
                    status_code=429,
                    detail="Project creation rate limit exceeded. Maximum 5 projects per hour. Please try again later."
                )
        except HTTPException:
            raise
        except Exception as e:
            log.error("project_rate_limit_check_failed", user_id=user_id, error=str(e))
            # Fail open (allow creation) if Redis is down, but log the error
        
        # ========================================================================
        # FRAUD PREVENTION #2: PROJECT LIMIT ENFORCEMENT BY TIER
        # ========================================================================
        # Prevent DOS attack: Attacker creates 1,000,000 projects to fill database
        # (storage growth + service degradation).
        #
        # Limits by plan tier (a quota level the operator assigns in the
        # database): see PROJECT_LIMITS below.
        # ========================================================================
        
        # Get user's subscription tier
        subscription_res = await core.resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/subscriptions",
            params={
                "select": "plan_tier,status",
                "user_id": f"eq.{user_id}",
                "status": "eq.active",
                "limit": "1"
            },
            headers=get_supabase_headers()
        )
        subscription_res.raise_for_status()
        subscriptions = subscription_res.json()
        
        # Determine tier (default: free)
        tier = 'free'
        if subscriptions and len(subscriptions) > 0:
            tier = subscriptions[0].get('plan_tier') or 'free'
        
        # Keep in sync with packages/ui/src/app/pricing/page.tsx (plan limits page)
        # and packages/engine/routes/billing.py PLAN_LIMITS
        PROJECT_LIMITS = {
            'free': 1,           # Free tier: 1 project only
            'starter': 5,        # Starter: 5 projects
            'growth': 20,        # Growth: 20 projects
            'business': 50,      # Business: bounded cap
            'enterprise': 100    # Enterprise: bounded, configurable
        }

        EXTRA_PROJECT_INCREMENT = int(os.getenv("EXTRA_PROJECT_ADDON_INCREMENT", "10"))
        PROJECT_HARD_MAX = int(os.getenv("PROJECT_HARD_MAX", "200"))

        addons = await get_org_add_ons(organization_id)
        has_extra = "extra_project" in addons

        max_projects = compute_project_limit(
            PROJECT_LIMITS.get(tier, 1),
            has_extra=has_extra,
            increment=EXTRA_PROJECT_INCREMENT,
            hard_max=PROJECT_HARD_MAX,
        )
        
        # Count existing projects for this organization
        count_res = await core.resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/projects",
            params={
                "select": "id",
                "organization_id": f"eq.{organization_id}",
                "status": "neq.deleted",
            },
            headers=get_supabase_headers(prefer="count=exact")
        )
        count_res.raise_for_status()
        
        # Extract count from Content-Range header (format: "0-9/10" where 10 is total)
        content_range = count_res.headers.get('Content-Range', '0-0/0')
        existing_count = int(content_range.split('/')[-1])
        
        # Enforce limit
        if existing_count >= max_projects:
            # Keep tier names in sync with the plan limits page
            tier_names = {
                'free': 'Free',
                'starter': 'Starter',
                'growth': 'Growth',
                'business': 'Business',
                'enterprise': 'Enterprise'
            }
            current_tier_name = tier_names.get(tier, 'Free')
            
            log.warning(
                "project_limit_reached", 
                user_id=user_id, 
                organization_id=organization_id, 
                tier=tier, 
                existing_count=existing_count, 
                max_projects=max_projects
            )
            
            raise HTTPException(
                status_code=403,
                detail=f"Project limit reached. Your {current_tier_name} plan allows {max_projects} project(s). You currently have {existing_count}. An operator can raise the plan tier to allow more projects."
            )
        
        # A row in projects (the schema has no fn_create_project). The budget starts
        # at 0, no cap; the owner sets it with PUT /management/project/{id}.
        res = await core.resources.http_client.post(
            f"{SUPABASE_URL}/rest/v1/projects",
            headers=get_supabase_headers(prefer="return=representation"),
            json={"organization_id": organization_id, "user_id": user_id, "name": req.name},
        )
        res.raise_for_status()
        created = (res.json() or [{}])[0]
        log.info("project_created", user_id=user_id, organization_id=organization_id, project_id=created.get("id"), tier=tier)
        return {"id": created.get("id"), "name": created.get("name"), "organization_id": organization_id,
                "budget_limit": created.get("budget_limit")}
    except HTTPException:
        raise
    except Exception as e:
        log.error("project_create_failed", user_id=user_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Project creation failed.")

@app.put("/management/project/{project_id}")
@limiter.limit(f"{RATE_LIMIT_MANAGEMENT_PER_MINUTE}/minute")
async def management_update_project(request: Request, project_id: str, req: ProjectUpdateRequest, user_id: str = Depends(get_current_user_id)):
    """
    Update a project's name and budget.
    Requires admin or owner role in the project's organization.
    """
    try:
        # Get project's organization
        proj_res = await core.resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/projects",
            params={
                "select": "organization_id",
                "id": f"eq.{project_id}",
                "status": "neq.deleted",
                "limit": "1"
            },
            headers=get_supabase_headers()
        )
        proj_res.raise_for_status()
        projects = proj_res.json()
        
        if not projects:
            raise HTTPException(status_code=404, detail="Project not found")
        
        organization_id = projects[0]["organization_id"]
        
        # Check if user has admin or owner role
        has_permission = await check_organization_permission(
            user_id,
            organization_id,
            required_role="admin"
        )
        
        if not has_permission:
            raise HTTPException(
                status_code=403,
                detail="Only admins and owners can update projects"
            )
        
        # The row itself (the schema has no fn_update_project). Then the cached
        # keys of the project are dropped, so the next request reads the new cap.
        res = await core.resources.http_client.patch(
            f"{SUPABASE_URL}/rest/v1/projects",
            params={"id": f"eq.{project_id}", "organization_id": f"eq.{organization_id}", "status": "neq.deleted"},
            headers=get_supabase_headers(prefer="return=representation"),
            json={"name": req.name, "budget_limit": str(req.budget_limit),
                  "updated_at": datetime.now(timezone.utc).isoformat()},
        )
        res.raise_for_status()
        if not res.json():
            raise HTTPException(status_code=404, detail="Project not found")
        
        await clear_project_cache(project_id)
        
        log.info("project_updated", user_id=user_id, project_id=project_id, organization_id=organization_id, new_name=req.name, new_budget=req.budget_limit)
        return {"status": "updated"}
    except HTTPException:
        raise
    except Exception as e:
        log.error("project_update_failed", user_id=user_id, project_id=project_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Project update failed.")

@app.get("/organizations")
@limiter.limit(f"{RATE_LIMIT_MANAGEMENT_PER_MINUTE}/minute")
async def list_user_organizations(request: Request, user_id: str = Depends(get_current_user_id)):
    """
    List all organizations where the current user is an active member.
    
    Returns:
        List of organizations with user's role in each.
    """
    try:
        user_orgs = await get_user_organizations(user_id)
        
        if not user_orgs:
            return []
        
        # Transform to clean response format
        result = []
        # get_user_organizations returns each membership with the organization's
        # columns merged in (id, name, owner_user_id), not nested under "organizations".
        for org in user_orgs:
            result.append({
                "id": org.get("id") or org.get("organization_id"),
                "name": org.get("name"),
                "role": org.get("role"),
                "is_owner": org.get("owner_user_id") == user_id
            })
        
        return result
    except Exception as e:
        log.error("list_organizations_failed", user_id=user_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list organizations")


@app.delete("/management/project/{project_id}")
@limiter.limit(f"{RATE_LIMIT_MANAGEMENT_PER_MINUTE}/minute")
async def management_delete_project(request: Request, project_id: str, user_id: str = Depends(get_current_user_id)):
    """
    Delete a project: its keys stop at once and it leaves every listing. The row
    and its request logs stay (deactivated, not erased).
    Requires admin or owner role in the project's organization.
    """
    try:
        # Get project's organization
        proj_res = await core.resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/projects",
            params={
                "select": "organization_id",
                "id": f"eq.{project_id}",
                "status": "neq.deleted",
                "limit": "1"
            },
            headers=get_supabase_headers()
        )
        proj_res.raise_for_status()
        projects = proj_res.json()
        
        if not projects:
            raise HTTPException(status_code=404, detail="Project not found")
        
        organization_id = projects[0]["organization_id"]
        
        # Check if user has admin or owner role
        has_permission = await check_organization_permission(
            user_id,
            organization_id,
            required_role="admin"
        )
        
        if not has_permission:
            raise HTTPException(
                status_code=403,
                detail="Only admins and owners can delete projects"
            )
        
        # Deactivated, not erased: request_logs, agents and agent sessions reference
        # the project with ON DELETE CASCADE, so a DELETE would take its audit
        # trail with it. Its keys stop first, then the project leaves every
        # listing, then its cached keys are dropped (after the database, so a
        # request in between cannot cache a key again).
        now = datetime.now(timezone.utc).isoformat()
        keys_res = await core.resources.http_client.patch(
            f"{SUPABASE_URL}/rest/v1/api_keys",
            params={"project_id": f"eq.{project_id}"},
            headers=get_supabase_headers(prefer="return=minimal"),
            json={"is_active": False},
        )
        keys_res.raise_for_status()
        res = await core.resources.http_client.patch(
            f"{SUPABASE_URL}/rest/v1/projects",
            params={"id": f"eq.{project_id}", "organization_id": f"eq.{organization_id}"},
            headers=get_supabase_headers(prefer="return=minimal"),
            json={"status": "deleted", "updated_at": now},
        )
        res.raise_for_status()
        await clear_project_cache(project_id)

        log.info("project_deleted", user_id=user_id, project_id=project_id, organization_id=organization_id)
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        log.error("project_delete_failed", user_id=user_id, project_id=project_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Project deletion failed.")

@app.post("/management/key")
@limiter.limit(f"{RATE_LIMIT_MANAGEMENT_PER_MINUTE}/minute")
async def management_create_key(
    request: Request, 
    project_id: str = Query(..., alias="projectId"),
    name: str = Query("Default", min_length=1, max_length=50), 
    user_id: str = Depends(get_current_user_id)
):
    """
    Create a new API key for a project.
    Any organization member can create keys (developer role sufficient).
    """
    try:
        # Get project's organization
        proj_res = await core.resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/projects",
            params={
                "select": "organization_id",
                "id": f"eq.{project_id}",
                "status": "neq.deleted",
                "limit": "1"
            },
            headers=get_supabase_headers()
        )
        proj_res.raise_for_status()
        projects = proj_res.json()
        
        if not projects:
            raise HTTPException(status_code=404, detail="Project not found")
        
        organization_id = projects[0]["organization_id"]
        
        # Check if user is member of organization (any role can create keys)
        is_member = await check_organization_permission(
            user_id,
            organization_id,
            required_role="developer"
        )
        
        if not is_member:
            raise HTTPException(
                status_code=403,
                detail="You are not a member of this organization"
            )
        
        clean_name = sanitize_input(name)
        if not clean_name:
            clean_name = "Default"
            
        new_key, key_prefix = generate_oxy_key()
        new_hash = hash_key(new_key)
        
        # Create key with created_by field
        res = await core.resources.http_client.post(
            f"{SUPABASE_URL}/rest/v1/api_keys",
            headers=get_supabase_headers(prefer="return=representation"),
            json={
                "project_id": project_id,
                "created_by": user_id,
                "key_prefix": key_prefix,
                "key_hash": new_hash,
                "key_name": clean_name  # FIX: Column is 'key_name', not 'name'
            }
        )
        
        # DEBUG: Enhanced error logging to diagnose 400 Bad Request from Supabase
        if res.status_code != 201:
            log.error(
                "supabase_key_create_error",
                status=res.status_code,
                body=res.text,
                project_id=project_id,
                user_id=user_id,
                key_prefix=key_prefix
            )
            raise HTTPException(
                status_code=500, 
                detail=f"Failed to create API key: {res.status_code}"
            )
        
        new_record = res.json()[0]
        new_key_id = new_record['id']
        
        async with core.resources.redis.pipeline(transaction=True) as pipe:
            # Hashes only: a key must never be readable from Redis.
            pipe.sadd(project_keys_set_name(project_id), new_hash)
            await pipe.execute()

        log.info("api_key_created", user_id=user_id, organization_id=organization_id, project_id=project_id, key_name=clean_name, key_prefix=key_prefix, key_id=new_key_id)
        return {"api_key": new_key}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        # DEBUG: Capture full Supabase error response
        log.error(
            "key_create_http_error",
            user_id=user_id,
            project_id=project_id,
            status_code=e.response.status_code,
            response_body=e.response.text,
            error_type=type(e).__name__
        )
        raise HTTPException(status_code=500, detail="Key creation failed.")
    except Exception as e:
        log.error("key_create_failed", user_id=user_id, project_id=project_id, error=str(e), error_type=type(e).__name__, exc_info=True)
        raise HTTPException(status_code=500, detail="Key creation failed.")

@app.get("/management/keys")
@limiter.limit(f"{RATE_LIMIT_MANAGEMENT_PER_MINUTE}/minute")
async def management_list_keys(
    request: Request, 
    project_id: str | None = Query(None, alias="projectId"),
    user_id: str = Depends(get_current_user_id)
):
    """
    List all API keys for a project.
    If projectId is not provided, uses the user's first project.
    Requires organization membership.
    """
    try:
        # Return mock API keys for internal sandbox/test project
        if project_id == "demo-project-id":
            log.info("sandbox_keys_mock", user_id=user_id)
            return [
                {
                    "id": "demo-key-1",
                    "key_prefix": "oxy_live_demo",
                    "name": "Demo API Key",  # Frontend expects 'name', backend maps from 'key_name'
                    "created_at": "2024-01-01T00:00:00Z",
                    "is_demo": True
                }
            ]
        
        # If no project_id provided, fetch user's first project
        if not project_id:
            user_orgs = await get_user_organizations(user_id)
            if not user_orgs:
                raise HTTPException(status_code=404, detail="No organizations found for user")
            
            org_ids = [org["organization_id"] for org in user_orgs]
            
            # Get user's first project
            projects_res = await core.resources.http_client.get(
                f"{SUPABASE_URL}/rest/v1/projects",
                params={
                    "select": "id,organization_id",
                    "organization_id": f"in.({','.join(org_ids)})",
                    "status": "neq.deleted",
                    "order": "created_at.asc",
                    "limit": "1"
                },
                headers=get_supabase_headers()
            )
            projects_res.raise_for_status()
            user_projects = projects_res.json()
            
            if not user_projects:
                raise HTTPException(status_code=404, detail="No projects found for user")
            
            project_id = user_projects[0]["id"]
            organization_id = user_projects[0]["organization_id"]
        else:
            # Get project's organization
            proj_res = await core.resources.http_client.get(
                f"{SUPABASE_URL}/rest/v1/projects",
                params={
                    "select": "organization_id",
                    "id": f"eq.{project_id}",
                    "status": "neq.deleted",
                    "limit": "1"
                },
                headers=get_supabase_headers()
            )
            proj_res.raise_for_status()
            projects = proj_res.json()
            
            if not projects:
                raise HTTPException(status_code=404, detail="Project not found")
            
            organization_id = projects[0]["organization_id"]
        
        # Check if user is member
        is_member = await check_organization_permission(
            user_id,
            organization_id,
            required_role="developer"
        )
        
        if not is_member:
            raise HTTPException(
                status_code=403,
                detail="You are not a member of this organization"
            )
        
        # List keys
        # Use 'key_name' column (not 'name' - was removed in schema)
        res = await core.resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/api_keys",
            params={
                "select": "id,key_prefix,key_name,created_at,last_used_at,is_active",
                "project_id": f"eq.{project_id}",
                "order": "created_at.desc"
            },
            headers=get_supabase_headers()
        )
        res.raise_for_status()
        
        # Map 'key_name' to 'name' for frontend compatibility
        keys = res.json()
        for key in keys:
            key["name"] = key.pop("key_name", key.get("name", "Unnamed"))
        return keys
    except HTTPException:
        raise
    except Exception as e:
        log.error("key_list_failed", user_id=user_id, project_id=project_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list keys.")

@app.delete("/management/key/{key_id}")
@limiter.limit(f"{RATE_LIMIT_MANAGEMENT_PER_MINUTE}/minute")
async def management_delete_key(
    request: Request, 
    key_id: str, 
    project_id: str = Query(..., alias="projectId"),
    user_id: str = Depends(get_current_user_id)
):
    """
    Revoke an API key: it stops at once and stays listed as inactive. Revoked,
    not deleted: request_logs keeps every row the key made.
    Requires organization membership.
    """
    try:
        # Get project's organization
        proj_res = await core.resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/projects",
            params={
                "select": "organization_id",
                "id": f"eq.{project_id}",
                "status": "neq.deleted",
                "limit": "1"
            },
            headers=get_supabase_headers()
        )
        proj_res.raise_for_status()
        projects = proj_res.json()
        
        if not projects:
            raise HTTPException(status_code=404, detail="Project not found")
        
        organization_id = projects[0]["organization_id"]
        
        # Check if user is member
        is_member = await check_organization_permission(
            user_id,
            organization_id,
            required_role="developer"
        )
        
        if not is_member:
            raise HTTPException(
                status_code=403,
                detail="You are not a member of this organization"
            )
        
        # Revoked, not deleted: request_logs.api_key_id points at this row, and a
        # DELETE cascaded to every audit row the key had made (1.0.2 and before).
        # Database first, then the cached record by the row's key_hash: a request
        # in between still finds the cache, one after it finds neither.
        res = await core.resources.http_client.patch(
            f"{SUPABASE_URL}/rest/v1/api_keys",
            params={"id": f"eq.{key_id}", "project_id": f"eq.{project_id}"},
            headers=get_supabase_headers(prefer="return=representation"),
            json={"is_active": False, "deactivated_reason": "revoked",
                  "deactivated_at": datetime.now(timezone.utc).isoformat()},
        )
        res.raise_for_status()
        rows = res.json() or []
        if not rows:
            log.warn("key_delete_not_found", user_id=user_id, key_id=key_id)
            raise HTTPException(status_code=404, detail="Key not found or user mismatch")
        try:
            await forget_cached_api_key(rows[0].get("key_hash"), project_id)
        except Exception:
            log.error("key_delete_cache_clear_failed", key_id=key_id, project_id=project_id, exc_info=True)
            raise HTTPException(
                status_code=503,
                detail="The key was revoked, but its cached record could not be cleared: it can work for up to 5 more minutes.",
            )

        log.info("api_key_revoked", user_id=user_id, key_id=key_id, project_id=project_id)
        return {"status": "revoked"}
    except HTTPException:
        raise  # 403, 404 and 503 above keep their status
    except Exception as e:
        log.error("key_delete_failed", user_id=user_id, key_id=key_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Key deletion failed.")

@app.get("/management/stats")
@limiter.limit(f"{RATE_LIMIT_MANAGEMENT_PER_MINUTE}/minute")
async def management_stats(
    request: Request, 
    project_id: str | None = Query(None, alias="projectId"),
    user_id: str = Depends(get_current_user_id)
):
    """
    Get project statistics (total requests, threats blocked).
    If projectId is not provided, uses the user's first project.
    Requires organization membership.
    """
    try:
        # Return mock stats for internal sandbox/test project
        if project_id == "demo-project-id":
            log.info("sandbox_stats_mock", user_id=user_id)
            return {
                "total_requests": 1247,
                "threats_blocked": 3,
                "total_cost": 4.20,
                "average_latency_ms": 243,
                "cache_hit_rate": 0.72,
                "top_models": [
                    {"model": "gpt-4", "requests": 892, "cost": 3.12},
                    {"model": "claude-3-sonnet", "requests": 355, "cost": 1.08}
                ]
            }
        
        # If no project_id provided, fetch user's first project
        if not project_id:
            user_orgs = await get_user_organizations(user_id)
            if not user_orgs:
                raise HTTPException(status_code=404, detail="No organizations found for user")
            
            org_ids = [org["organization_id"] for org in user_orgs]
            
            # Get user's first project
            projects_res = await core.resources.http_client.get(
                f"{SUPABASE_URL}/rest/v1/projects",
                params={
                    "select": "id,organization_id",
                    "organization_id": f"in.({','.join(org_ids)})",
                    "status": "neq.deleted",
                    "order": "created_at.asc",
                    "limit": "1"
                },
                headers=get_supabase_headers()
            )
            projects_res.raise_for_status()
            user_projects = projects_res.json()
            
            if not user_projects:
                raise HTTPException(status_code=404, detail="No projects found for user")
            
            project_id = user_projects[0]["id"]
            organization_id = user_projects[0]["organization_id"]
        else:
            # Get project's organization
            proj_res = await core.resources.http_client.get(
                f"{SUPABASE_URL}/rest/v1/projects",
                params={
                    "select": "organization_id",
                    "id": f"eq.{project_id}",
                    "status": "neq.deleted",
                    "limit": "1"
                },
                headers=get_supabase_headers()
            )
            proj_res.raise_for_status()
            projects = proj_res.json()
            
            if not projects:
                raise HTTPException(status_code=404, detail="Project not found")
            
            organization_id = projects[0]["organization_id"]
        
        # Check if user is member
        is_member = await check_organization_permission(
            user_id,
            organization_id,
            required_role="developer"
        )
        
        if not is_member:
            raise HTTPException(
                status_code=403,
                detail="You are not a member of this organization"
            )
        
        # Get stats via RPC function
        res = await core.resources.http_client.post(
             f"{SUPABASE_URL}/rest/v1/rpc/fn_get_project_stats",
             headers=get_supabase_headers(),
             json={"p_project_id": project_id}
        )
        res.raise_for_status()
        
        data = res.json()
        if not data:
             return {"total_requests": 0, "threats_blocked": 0}
             
        stats = data[0]
        return {
            "total_requests": stats.get("total_requests", 0),
            "threats_blocked": stats.get("threats_blocked", 0)
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error("stats_failed_rpc", user_id=user_id, project_id=project_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve stats.")

def redact_dict_recursive(data):
    if isinstance(data, dict):
        return {k: redact_dict_recursive(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [redact_dict_recursive(item) for item in data]
    elif isinstance(data, str):
        if OPENAI_KEY_REGEX.search(data):
            return OPENAI_KEY_REGEX.sub("[REDACTED_SECRET]", data)
    return data

def get_cache_key(project_id: str, payload: dict) -> str:
    payload_str = json.dumps(payload, sort_keys=True)
    hash_input = f"{project_id}:{payload_str}"
    return hashlib.sha256(hash_input.encode()).hexdigest()

# Rate limiting key function for API keys
def get_rate_limit_key(request: Request) -> str:
    """Get rate limit key based on API key from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header[7:]
        # Use a hash of the key prefix for rate limiting (privacy + performance)
        if api_key.startswith("oxy_live_") and len(api_key) >= KEY_PREFIX_LENGTH:
            key_prefix = api_key[:KEY_PREFIX_LENGTH]
            return f"rate_limit:{hashlib.sha256(key_prefix.encode()).hexdigest()[:16]}"
    # Fallback to IP address if no valid API key
    return get_remote_address(request)


# ============================================================================
# BURST RATE LIMITER (SECURITY HARDENING)
# ============================================================================
# Purpose: Prevent rapid-fire attacks (e.g., 100 requests in 1 second)
# Why: SlowAPI only checks per-minute limits, allowing short bursts
# Attack Vector: Attacker sends 60 requests in 1 second, then waits 59 seconds
# Solution: Secondary "burst" check with 10 requests/second limit
# 
# Effect:
# - Prevents DDoS-style rapid-fire attacks
# - Protects infrastructure from spike overload
# - No impact on legitimate users (humans can't type that fast)
# 
# Technical Implementation:
# - Redis sliding window (10 requests per 1 second)
# - Separate key namespace: burst_limit:{user_id}
# - Runs BEFORE main rate limiter (fail-fast)
# ============================================================================

async def check_burst_rate_limit(request: Request, user_id: str) -> None:
    """
    Check burst rate limit (10 requests per second).
    
    This prevents rapid-fire attacks where an attacker sends many requests
    in a short burst, even if they're within the per-minute limit.
    
    Example Attack:
    - Without burst limit: Send 60 req in 1 sec, wait 59 sec, repeat
    - With burst limit: Blocked after 10 req in 1 sec
    
    Args:
        request: FastAPI request object
        user_id: User ID or IP address for rate limiting
    
    Raises:
        HTTPException: 429 if burst limit exceeded
    """
    try:
        burst_key = f"burst_limit:{user_id}"
        window = 1  # 1 second window
        limit = 10  # Max 10 requests per second
        
        # Use Redis for burst tracking (if available)
        if resources.redis:
            # Increment counter
            count = await resources.redis.incr(burst_key)
            
            # Set TTL on first request
            if count == 1:
                await resources.redis.expire(burst_key, window)
            
            # Check limit
            if count > limit:
                log.warning(
                    "burst_rate_limit_exceeded",
                    user_id=user_id,
                    count=count,
                    limit=limit,
                    ip=request.client.host if request.client else "unknown"
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Burst rate limit exceeded. Max {limit} requests per second. Please slow down."
                )
        else:
            # Redis unavailable: Use in-memory fallback (per-worker, less accurate but better than nothing)
            if not hasattr(check_burst_rate_limit, "_burst_cache"):
                from collections import deque
                check_burst_rate_limit._burst_cache = {}  # type: ignore
            
            now = datetime.now(timezone.utc).timestamp()
            
            if burst_key not in check_burst_rate_limit._burst_cache:  # type: ignore
                check_burst_rate_limit._burst_cache[burst_key] = deque(maxlen=limit)  # type: ignore
            
            timestamps = check_burst_rate_limit._burst_cache[burst_key]  # type: ignore
            
            # Remove timestamps older than 1 second
            while timestamps and timestamps[0] < now - window:
                timestamps.popleft()
            
            # Check limit
            if len(timestamps) >= limit:
                log.warning(
                    "burst_rate_limit_exceeded_fallback",
                    user_id=user_id,
                    count=len(timestamps),
                    limit=limit
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Burst rate limit exceeded. Max {limit} requests per second."
                )
            
            # Add current timestamp
            timestamps.append(now)
            
    except HTTPException:
        raise  # Re-raise rate limit errors
    except Exception as e:
        # Log error but don't block request (fail-open for infrastructure errors)
        log.error("burst_rate_limit_check_failed", error=str(e), exc_info=True)


@app.get("/metrics")
async def metrics(credentials: HTTPBasicCredentials = Depends(HTTPBasic())):
    """
    Prometheus metrics endpoint (SECURED).
    Exports operational metrics for monitoring and alerting.
    
    Requires HTTP Basic Authentication: without it, metrics expose
    spend, user count, request volume and costs to anyone who can reach the port.
    
    Environment variables required:
    - METRICS_USER: Username for Basic Auth (default: "admin")
    - METRICS_PASSWORD: Password for Basic Auth (default: "changeme")
    
    Access via: curl -u admin:password http://localhost:8000/metrics
    Then configure Prometheus to scrape with basic_auth section.
    """
    # SECURITY FIX: Verify credentials using constant-time comparison
    metrics_user = os.getenv("METRICS_USER", "admin")
    metrics_password = os.getenv("METRICS_PASSWORD", "changeme")
    
    correct_username = secrets.compare_digest(credentials.username, metrics_user)
    correct_password = secrets.compare_digest(credentials.password, metrics_password)
    
    if not (correct_username and correct_password):
        log.warn("metrics_auth_failed", 
                username=credentials.username,
                message="Unauthorized metrics access attempt")
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic realm=\"Metrics\""}
        )
    
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )


@app.get("/health")
async def health(request: Request): 
    """
    Health check endpoint with dependency validation.
    
    Readiness behaviour:
    - Returns 503 (Service Unavailable) if Redis is down
    - Load balancers use this to stop routing traffic to unhealthy pods
    - Prevents cascading failures by failing fast
    
    CRITICAL: Redis failure = immediate 503 (not 200 with "degraded")
    Rationale: Without Redis, budget checks cannot be enforced
    
    Returns:
        200 OK: All dependencies healthy
        503 Service Unavailable: Redis down (critical) or Supabase down
    """
    health_status = {
        "status": "active",
        "version": "1.0.5",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    redis_healthy = False
    supabase_healthy = False
    
    # CRITICAL: Check Redis connection (required for budget, cache, rate limiting)
    try:
        await core.resources.redis.ping()
        health_status["redis"] = "healthy"
        redis_healthy = True
    except Exception as e:
        log.error("health_check_redis_failed", 
                 error=str(e), 
                 message="Redis is DOWN. Service unavailable.")
        health_status["redis"] = "unhealthy"
        health_status["status"] = "unavailable"
    
    # IMPORTANT: Check Supabase connection (required for auth, billing, logs)
    try:
        test_response = await core.resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/",
            headers=get_supabase_headers(),
            timeout=5.0
        )
        if test_response.status_code < 500:
            health_status["supabase"] = "healthy"
            supabase_healthy = True
        else:
            health_status["supabase"] = "unhealthy"
            health_status["status"] = "unavailable"
    except Exception as e:
        log.error("health_check_supabase_failed", 
                 error=str(e),
                 message="Supabase is DOWN. Service unavailable.")
        health_status["supabase"] = "unhealthy"
        health_status["status"] = "unavailable"
    
    # PRODUCTION RULE: Return 503 if ANY critical dependency is down
    # Load balancers will stop routing traffic to this pod
    if redis_healthy and supabase_healthy:
        status_code = 200
        health_status["status"] = "healthy"
    else:
        status_code = 503
        health_status["status"] = "unavailable"
    
    return JSONResponse(content=health_status, status_code=status_code)


@app.get("/csrf-debug")
async def csrf_debug(request: Request):
    """
    CSRF Cookie Diagnostic Endpoint (Development Only)
    
    Returns information about CSRF cookie configuration and current state.
    Helps debug cookie issues in incognito mode or localhost.
    
    SECURITY: Should be disabled or gated in production
    """
    import os
    
    environment = os.getenv("ENVIRONMENT", "development")
    is_production = environment == "production"
    
    # Get current CSRF cookie
    csrf_cookie = request.cookies.get("csrf_token")
    
    return {
        "environment": environment,
        "is_production": is_production,
        "cookie_settings": {
            "secure": is_production,
            "httponly": False,  # Must be False for CSRF to work
            "samesite": "lax",
            "max_age": 86400,
            "path": "/",
            "domain": None  # Important for localhost
        },
        "current_csrf_cookie": {
            "exists": csrf_cookie is not None,
            "value_preview": csrf_cookie[:10] + "..." if csrf_cookie else None,
            "length": len(csrf_cookie) if csrf_cookie else 0
        },
        "request_info": {
            "protocol": request.url.scheme,
            "host": request.url.hostname,
            "port": request.url.port,
            "is_localhost": request.url.hostname in ["localhost", "127.0.0.1", "0.0.0.0"],
            "user_agent": request.headers.get("user-agent", "")[:50]
        },
        "troubleshooting": {
            "secure_on_http": is_production and request.url.scheme == "http",
            "advice": [
                "If running localhost, ensure ENVIRONMENT is not set to 'production'",
                "Incognito mode may block third-party cookies - this app uses first-party",
                "Check browser console for cookie warnings",
                "Ensure requests use http://localhost (not 127.0.0.1 or 0.0.0.0)"
            ]
        }
    }