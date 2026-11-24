"""
Proxy Route - AI Gateway Core
==============================

This module handles the /v1/chat/completions endpoint - the main request
path of the Oxy gateway. Extracted from main.py for better modularity.

CRITICAL: This is the hot path. Any changes must be tested thoroughly.

Features:
- Multi-provider support (OpenAI, Anthropic, Google, Ollama, vLLM)
- Budget management with atomic Redis operations
- Response caching for cost optimization
- Rate limiting (per-minute + burst protection)
- Dead key detection
- Audit logging
- 🆕 Tool/Function call support with DLP scanning
- 🆕 Agent session tracking
- 🆕 Local model routing (Ollama/vLLM)

Security:
- API key authentication (via dependencies)
- Organization-level budgets
- RLS enforcement via Supabase
- DLP scanning (sensitive data detection)
- 🆕 Tool input/output DLP scanning
- 🆕 Tool permission enforcement (RBAC)

Performance:
- Redis caching (potential cost savings on repeated requests; depends on workload)
- HTTP/2 connection pooling
- Circuit breakers for provider failures
- Streaming support for real-time responses

Machine-readable error responses:
- All errors return JSON with: code, message, hint, retry_after (if applicable)
- AI agents can self-heal by reading the hint field
"""

import json
import hashlib
import datetime
import time
import asyncio
import os
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, Depends
from fastapi.responses import Response, StreamingResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from slowapi import Limiter
from slowapi.util import get_remote_address
import httpx

# Core imports
from core import (
    log,
    resources,
    SUPABASE_URL,
    RATE_LIMIT_PER_MINUTE,
    DLP_PATTERNS,  # DLP scanning patterns (ALWAYS ENABLED)
)
from core.control import ControlUnavailable, check_execution_allowed, is_session_terminated, get_org_status
from dependencies import (
    build_api_key_dependency,
    get_user_and_key_from_api_key,
    get_supabase_headers,
    CacheData,
    auth_scheme,
    hash_key,
)
from security_utils import log_error_safe
from providers import provider_registry, ProviderHTTPError, PricingEngine
from core.costs import calculate_cost, record_request
from core.privacy_logging import DEFAULT_MODE
import re  # For DLP regex scanning
from routes.usage import get_org_monthly_usage, increment_org_monthly_usage, current_month_slug

# Router setup
router = APIRouter(prefix="/v1", tags=["proxy"])
api_key_dependency = build_api_key_dependency(lambda: get_user_and_key_from_api_key)

# Rate limiter
limiter = Limiter(key_func=get_remote_address)


# ============================================================================
# MACHINE-READABLE ERROR RESPONSES
# ============================================================================
# Structured errors that AI agents can parse and self-heal from.
# Every error includes: code, message, hint (for AI), retry_after (if applicable)
# ============================================================================

def ai_error(
    status_code: int,
    code: str,
    message: str,
    hint: str,
    retry_after: int | None = None,
    context: dict | None = None
) -> HTTPException:
    """
    Create an AI-readable error response.
    
    Args:
        status_code: HTTP status code
        code: Machine-readable error code (e.g., "BUDGET_EXCEEDED")
        message: Human-readable message
        hint: Instruction for AI agents on how to handle this error
        retry_after: Seconds to wait before retrying (for 429 errors)
        context: Additional context (e.g., budget limits, rate limits)
    
    Returns:
        HTTPException with structured JSON detail
    """
    detail = {
        "error": {
            "code": code,
            "message": message,
            "hint": hint,
        }
    }
    if retry_after is not None:
        detail["error"]["retry_after"] = retry_after
    if context:
        detail["error"]["context"] = context
    
    return HTTPException(status_code=status_code, detail=detail)


async def _load_org_plan_and_flags(org_id: str) -> Dict[str, Any]:
    """
    Fetch organization plan and guardrail flags from Supabase.
    """
    try:
        res = await resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/organizations",
            params={
                "id": f"eq.{org_id}",
                "select": "subscription_plan,paused,auto_suspend_on_cost_spikes,last_paused_at,last_pause_reason",
                "limit": 1,
            },
            headers=get_supabase_headers(),
        )
        res.raise_for_status()
        rows = res.json()
        if not rows:
            return {
                "plan_tier": "free",
                "org_paused": None,
                "auto_suspend_on_cost_spikes": None,
            }
        row = rows[0]
        return {
            "plan_tier": row.get("subscription_plan") or "free",
            "org_paused": row.get("paused", False),
            "auto_suspend_on_cost_spikes": row.get("auto_suspend_on_cost_spikes", True),
            "last_paused_at": row.get("last_paused_at"),
            "last_pause_reason": row.get("last_pause_reason"),
        }
    except Exception as e:
        log_error_safe(log, "org_plan_fetch_failed", e, organization_id=org_id)
        return {
            "plan_tier": "free",
            "org_paused": None,
            "auto_suspend_on_cost_spikes": None,
        }


async def _load_org_privacy_mode(org_id: str) -> str:
    """Fetch organization privacy_mode (defaults to metadata_only)."""
    if getattr(resources, "http_client", None) is None:
        return DEFAULT_MODE
    try:
        res = await resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/organizations",
            params={
                "id": f"eq.{org_id}",
                "select": "privacy_mode",
                "limit": 1,
            },
            headers=get_supabase_headers(),
        )
        res.raise_for_status()
        rows = res.json()
        if rows:
            return rows[0].get("privacy_mode") or DEFAULT_MODE
    except Exception as e:
        log_error_safe(log, "org_privacy_fetch_failed", e, organization_id=org_id)
    return DEFAULT_MODE

# Guardrails: resolve paused status with multiple fallbacks (http_client → supabase → default False)
async def _resolve_org_paused(organization_id: str, key_data: CacheData) -> bool:
    cached = key_data.get("org_paused")
    if cached is True:
        return True

    paused: bool | None = None

    # Try Supabase client if available (sync client used in tests)
    sb = getattr(resources, "supabase", None)
    if sb is not None:
        try:
            res = (
                sb.table("organizations")
                .select("paused")
                .eq("id", organization_id)
                .limit(1)
                .execute()
            )
            if hasattr(res, "__await__"):  # the engine's client is async; test fakes may not be
                res = await res
            rows = getattr(res, "data", []) or []
            if rows:
                paused = bool(rows[0].get("paused", False))
                key_data["org_paused"] = paused
                return paused
        except Exception:
            pass

    # Fallback to cached False if set, else default False
    if cached is False:
        return False
    key_data["org_paused"] = bool(paused) if paused is not None else False
    return key_data["org_paused"]

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

# Local providers run on the operator's own hardware and most take no key. One
# started with an API key of its own (vLLM's --api-key) gets it the same two
# ways as a hosted provider: the organization's provider page, or <NAME>_API_KEY.
LOCAL_PROVIDERS = {"ollama", "vllm", "docker", "local"}
# Not a prefix any mock key uses (see MOCK_KEY_PREFIXES in the request path).
from providers.local_provider import NO_KEY_PLACEHOLDER as LOCAL_PROVIDER_PLACEHOLDER_KEY


async def get_provider_api_key(organization_id: str, provider_name: str) -> str:
    """The provider's key for this organization; a local provider that has none is not an error.

    Up to 1.0.6 a request for ollama/..., vllm/... or local/... answered 400
    "Provider not configured" for want of a key the model never needed.
    """
    try:
        return await _provider_key_from_config(organization_id, provider_name)
    except HTTPException as exc:
        if provider_name in LOCAL_PROVIDERS and exc.status_code == 400:
            return LOCAL_PROVIDER_PLACEHOLDER_KEY
        raise


async def _provider_key_from_config(organization_id: str, provider_name: str) -> str:
    """
    Fetch and DECRYPT the provider API key (OpenAI, Anthropic, Google) for an organization.
    
    SECURITY CRITICAL:
    - Provider keys are stored ENCRYPTED in Supabase Vault
    - The 'providers' table stores only a UUID reference (key_secret_id)
    - We must call vault.decrypted_secrets to get the actual plaintext key
    
    Args:
        organization_id: Organization UUID
        provider_name: Provider name ('openai', 'anthropic', 'google')
    
    Returns:
        Provider API key string (decrypted)
    
    Raises:
        HTTPException 400: If provider not configured for this organization
        HTTPException 500: If decryption fails
    """
    # Env-var fallback when no org-level provider key is configured. The
    # original code only honoured OPENAI_API_KEY; for a self-hosted
    # deployment any provider should be able to use the same pattern, so
    # also pick up ANTHROPIC_API_KEY and GOOGLE_API_KEY (or GEMINI_API_KEY)
    # when the dashboard hasn't been used to onboard a per-org key.
    _ENV_FALLBACK = {
        "openai": ("OPENAI_API_KEY",),
        "anthropic": ("ANTHROPIC_API_KEY",),
        "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        "ollama": ("OLLAMA_API_KEY",),
        "vllm": ("VLLM_API_KEY",),
        "docker": ("DOCKER_API_KEY",),
        "local": ("LOCAL_API_KEY",),
    }
    env_fallback_key = None
    for env_name in _ENV_FALLBACK.get(provider_name, ()):
        candidate = os.getenv(env_name)
        if candidate:
            env_fallback_key = candidate
            break
    
    try:
        # STEP 1: Get the vault secret ID from providers table
        res = await resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/providers",
            params={
                "select": "key_secret_id",
                "organization_id": f"eq.{organization_id}",
                "provider_name": f"eq.{provider_name}",
                "is_active": "eq.true",
                "limit": "1"
            },
            headers=get_supabase_headers(),
            timeout=10.0
        )
        
        if res.status_code != 200:
            log.error("provider_key_fetch_error", 
                     status=res.status_code, 
                     body=res.text,
                     organization_id=organization_id,
                     provider_name=provider_name)
            if env_fallback_key:
                log.warning(
                    "provider_key_env_fallback",
                    provider=provider_name,
                    reason="supabase_fetch_failed",
                    key_prefix=env_fallback_key[:10]
                )
                return env_fallback_key
            raise HTTPException(
                status_code=500, 
                detail="Failed to fetch provider configuration"
            )
        
        rows = res.json()
        if not rows:
            if env_fallback_key:
                log.info(
                    "provider_key_env_fallback",
                    provider=provider_name,
                    reason="not_configured_in_db",
                    key_prefix=env_fallback_key[:10]
                )
                return env_fallback_key
            env_var_hint = {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "google": "GOOGLE_API_KEY",
            }.get(provider_name, f"{provider_name.upper()}_API_KEY")
            raise HTTPException(
                status_code=400,
                detail=f"Provider '{provider_name}' not configured. Add your API key in Settings or set {env_var_hint} for local development."
            )
        
        key_secret_id = rows[0].get('key_secret_id')
        if not key_secret_id:
            if env_fallback_key:
                log.warning(
                    "provider_key_env_fallback",
                    provider=provider_name,
                    reason="missing_key_reference",
                    key_prefix=env_fallback_key[:10]
                )
                return env_fallback_key
            raise HTTPException(
                status_code=500,
                detail=f"Provider key reference missing for {provider_name}"
            )
        
        # STEP 2: Decrypt the key using Supabase Vault RPC function
        # SECURITY: This calls the vault.decrypted_secrets view via RPC
        # The secret is decrypted server-side and returned over HTTPS
        vault_res = await resources.http_client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/fn_decrypt_provider_key",
            json={"p_secret_id": str(key_secret_id)},
            headers=get_supabase_headers(),
            timeout=10.0
        )
        
        if vault_res.status_code != 200:
            log.error("vault_decryption_failed",
                     status=vault_res.status_code,
                     body=vault_res.text[:200],  # Truncate for safety
                     organization_id=organization_id,
                     provider_name=provider_name)
            if env_fallback_key:
                log.warning(
                    "provider_key_env_fallback",
                    provider=provider_name,
                    reason="vault_decryption_failed",
                    key_prefix=env_fallback_key[:10]
                )
                return env_fallback_key
            raise HTTPException(
                status_code=500,
                detail=f"Failed to decrypt {provider_name} key from vault"
            )
        
        vault_data = vault_res.json()
        
        # Handle different response formats (object vs scalar)
        if isinstance(vault_data, dict):
            provider_key = vault_data.get('decrypted_secret') or vault_data.get('secret')
        elif isinstance(vault_data, str):
            provider_key = vault_data
        else:
            provider_key = None
        
        if not provider_key:
            log.error("vault_decryption_empty",
                     organization_id=organization_id,
                     provider_name=provider_name,
                     response_type=type(vault_data).__name__)
            if env_fallback_key:
                log.warning(
                    "provider_key_env_fallback",
                    provider=provider_name,
                    reason="vault_decryption_empty",
                    key_prefix=env_fallback_key[:10]
                )
                return env_fallback_key
            raise HTTPException(
                status_code=500,
                detail=f"Provider key decryption returned empty for {provider_name}"
            )
        
        log.info("provider_key_decrypted",
                organization_id=organization_id,
                provider_name=provider_name,
                key_prefix=provider_key[:8] + "..." if len(provider_key) > 8 else "[SHORT]")
        
        return provider_key
        
    except HTTPException:
        raise
    except Exception as e:
        log.error("provider_key_fetch_failed", 
                 error=str(e),
                 organization_id=organization_id,
                 provider_name=provider_name)
        if env_fallback_key:
            log.warning(
                "provider_key_env_fallback",
                provider=provider_name,
                reason="unexpected_error",
                key_prefix=env_fallback_key[:10]
            )
            return env_fallback_key
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve provider configuration"
        )

def get_rate_limit_key(request: Request) -> str:
    """Get rate limit key from API key (hashed, never the key itself) or IP."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header.replace("Bearer ", "").strip()
        return f"key:{hash_key(api_key)}"
    return f"ip:{get_remote_address(request)}"


async def check_burst_rate_limit(request: Request, user_id: str) -> None:
    """
    Burst rate limiter: 10 requests/second per user.
    
    Prevents rapid-fire attacks (DDoS, credential stuffing).
    SlowAPI only checks per-minute, allowing short bursts.
    """
    burst_key = f"burst:{user_id}"
    burst_window = 1  # 1 second
    # Per-user requests per second; BURST_LIMIT_PER_SECOND overrides (the proofs raise it).
    burst_limit = int(os.getenv("BURST_LIMIT_PER_SECOND", "10"))
    
    if resources.redis is None or not hasattr(resources.redis, "incr"):
        log.debug("burst_rate_limit_disabled", user_id=user_id)
        return
    try:
        current_count = await resources.redis.incr(burst_key)
        if current_count == 1:
            await resources.redis.expire(burst_key, burst_window)
        if current_count > burst_limit:
            log.warn(
                "burst_rate_limit_exceeded",
                user_id=user_id,
                count=current_count,
                limit=burst_limit,
            )
            raise ai_error(
                status_code=429,
                code="RATE_LIMITED",
                message=f"Rate limit exceeded: {burst_limit} requests per second.",
                hint="Implement exponential backoff. Start with 1 second delay, double on each retry. Max 5 retries.",
                retry_after=1,
                context={"limit": burst_limit, "window": "1 second"},
            )
    except HTTPException:
        raise
    except Exception as e:
        log.error("burst_rate_limit_check_failed", user_id=user_id, error=str(e)[:100])


def estimate_max_cost_for_model(model: str, num_messages: int) -> Decimal:
    """
    Estimate maximum possible cost for a request.
    
    Used for budget reservation (pessimistic estimate).
    Actual cost will be calculated after completion.
    """
    # Assume worst case: 4000 input + 2000 output tokens
    max_input_tokens = num_messages * 500 + 1000  # ~500 tokens per message + system
    max_output_tokens = 2000
    
    # Calculate max cost using pricing engine
    return PricingEngine.calculate_cost(model, max_input_tokens, max_output_tokens)


def _log_supabase_side_effect_failure(event: str, level: str = "warning", **fields) -> None:
    """
    Centralized logging for non-critical Supabase side effects (request logs, achievements).
    Keeps user traffic flowing while still emitting useful diagnostics.
    """
    logger = getattr(log, level, log.warning)
    logger(event, **fields)


async def log_request(
    user_id: str,
    project_id: str,
    organization_id: str,
    model: str,
    provider: str,
    status: str,
    threat: str,
    threat_type: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost: Decimal,
    subscription_id: str,
    reserved_amount: Decimal,
    *,
    privacy_mode: str | None = None,
    prompt_text: str | None = None,
    response_text: str | None = None,
    latency_ms: int | None = None,
    api_key_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
):
    """
    Backward-compatible wrapper around core.costs.record_request. The key, agent
    and session go on the row: without them a served request was not attributed
    to the key that made it.
    """
    await record_request(
        user_id=user_id,
        project_id=project_id,
        organization_id=organization_id,
        api_key_id=api_key_id,
        agent_id=agent_id,
        session_id=session_id,
        model=model,
        provider=provider,
        status=status,
        threat=threat,
        threat_type=threat_type,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        subscription_id=subscription_id,
        reserved_amount=reserved_amount,
        cache_hit=False,
        cost_tags={"project_id": project_id},
        privacy_mode=privacy_mode,
        prompt_text=prompt_text,
        response_text=response_text,
        latency_ms=latency_ms,
    )


KEY_USE_WINDOW_SECONDS = 60
_key_use_writes: set = set()  # references to the fire-and-forget writes, so none is collected early


async def touch_key_last_used(key_id: Optional[str]) -> bool:
    """Write api_keys.last_used_at for a key that just made a request, at most once
    a minute per key; True when this call wrote it.

    Nothing wrote the column before, so the dashboard's API Keys page said "Never"
    for every key, used or not. The minute is kept in Redis (SET NX EX), so a burst
    of requests costs one database write. When Redis or the database cannot answer,
    the write is skipped: it never holds up or fails the request it is about.
    """
    if not key_id or getattr(resources, "redis", None) is None or getattr(resources, "http_client", None) is None:
        return False
    try:
        first = await resources.redis.set("key_used:%s" % key_id, "1", nx=True, ex=KEY_USE_WINDOW_SECONDS)
    except Exception:
        return False
    if not first:
        return False
    try:
        res = await resources.http_client.patch(
            f"{SUPABASE_URL}/rest/v1/api_keys",
            params={"id": f"eq.{key_id}"},
            headers=get_supabase_headers(prefer="return=minimal"),
            json={"last_used_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            timeout=5.0,
        )
        res.raise_for_status()
        return True
    except Exception as e:
        log_error_safe(log, "key_last_used_write_failed", e, key_id=key_id)
        return False


def note_key_use(key_id: Optional[str]) -> None:
    """touch_key_last_used without waiting for it: the request goes on at once."""
    task = asyncio.create_task(touch_key_last_used(key_id))
    _key_use_writes.add(task)
    task.add_done_callback(_key_use_writes.discard)


async def _model_of(request: Request) -> str:
    """The model a request asked for, for the audit row of a request refused before
    its body is parsed; "unknown" when the body is not JSON or names none."""
    try:
        body = json.loads(await request.body())
        model = body.get("model") if isinstance(body, dict) else None
        return model if isinstance(model, str) and model else "unknown"
    except Exception:
        return "unknown"


async def record_blocked_request(
    *,
    key_data: CacheData,
    model: str,
    messages: list,
    reason: str,
    request_start_ms: float,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None:
    """Write a request_logs row for a request the gateway refused by policy: the
    budget, the kill switch, the burst limit, a suspended organization, a body
    that is not JSON. Such a refusal is not a threat (threat_detected false);
    DLP findings are recorded where DLP runs.

    Awaited rather than queued: these paths end in a raised error or an early
    response, and the audit trail must not depend on background tasks running.
    """
    try:
        await record_request(
            user_id=key_data["user_id"],
            project_id=key_data["project_id"],
            organization_id=key_data["organization_id"],
            api_key_id=key_data.get("key_id"),
            model=model,
            provider="blocked",
            status="blocked",
            threat="blocked",
            threat_type=reason,
            prompt_tokens=0,
            completion_tokens=0,
            cost=Decimal("0"),
            subscription_id=None,
            reserved_amount=Decimal("0"),
            cache_hit=False,
            # Agent and session ids on a refused request come from request headers and
            # are not verified to belong to this organization: keep them as claims in
            # cost_tags, never in the agent_id/session_id foreign-key columns.
            cost_tags={"project_id": key_data["project_id"], "claimed_agent_id": agent_id, "claimed_session_id": session_id},
            privacy_mode=key_data.get("privacy_mode") or DEFAULT_MODE,
            prompt_text=json.dumps(messages),
            response_text=None,
            latency_ms=int((time.monotonic() - request_start_ms) * 1000),
            threat_detected=False,
        )
    except Exception as e:
        log.warning("blocked_request_log_failed", reason=reason, error=str(e)[:200])


async def check_and_celebrate_first_request(user_id: str, cost: Decimal, model: str):
    """
    FIRST REQUEST ACHIEVEMENT
    =========================
    
    Check if this is the user's first API request.
    If so, create an achievement record that the dashboard shows once.
    
    Implementation:
    1. Check if user has any previous request_logs (count = 0?)
    2. If 0: Insert achievement record ("first_request")
    3. Frontend polls achievements table → detects new achievement
    4. Frontend shows <CelebrationModal />
    """
    try:
        if getattr(resources, "http_client", None) is None or getattr(resources, "supabase", None) is None:
            _log_supabase_side_effect_failure(
                "first_request_ceremony_skipped_supabase_missing",
                level="info",
                user_id=user_id,
            )
            return

        count_res = await resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/request_logs",
            params={
                "select": "id",
                "user_id": f"eq.{user_id}",
                "limit": "1"
            },
            headers=get_supabase_headers(prefer="count=exact"),
            timeout=5.0
        )
        
        if count_res.status_code != 200:
            _log_supabase_side_effect_failure(
                "first_request_check_failed_http",
                level="warning",
                user_id=user_id,
                status=count_res.status_code,
            )
            return
        
        content_range = count_res.headers.get('Content-Range', '0-0/0')
        total_count = int(content_range.split('/')[-1])
        
        if total_count <= 1:
            log.info("first_request_detected", user_id=user_id, model=model, cost=float(cost))
            
            achievement_data = {
                "cost_saved": float(cost),
                "model": model,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }
            
            try:
                resp = await resources.supabase.table("user_achievements").insert({
                    "user_id": user_id,
                    "achievement_type": "first_request",
                    "achievement_data": achievement_data
                }).execute()
                status_code = getattr(resp, "status_code", 200)
                if status_code == 409:
                    log.debug("first_request_achievement_already_exists", user_id=user_id)
                elif status_code and status_code >= 400:
                    _log_supabase_side_effect_failure(
                        "first_request_achievement_failed_soft",
                        level="warning",
                        user_id=user_id,
                        status_code=status_code,
                    )
                else:
                    log.info("first_request_achievement_created", 
                            user_id=user_id, 
                            cost_saved=float(cost))
            except Exception as e:
                if "duplicate key" in str(e).lower() or "unique constraint" in str(e).lower() or "409" in str(e):
                    log.debug("first_request_achievement_already_exists", user_id=user_id)
                else:
                    _log_supabase_side_effect_failure(
                        "first_request_achievement_failed_soft",
                        level="warning",
                        user_id=user_id,
                        error=str(e)[:200],
                    )
        
    except Exception:
        # Fail silently - don't block request if ceremony fails
        _log_supabase_side_effect_failure(
            "first_request_ceremony_failed_soft",
            level="warning",
            user_id=user_id,
        )



# ============================================================================
# DLP SCANNER (ALWAYS ENABLED - NO DEV MODE BYPASS)
# ============================================================================
# CRITICAL: This scanner runs ALWAYS, even on localhost/development.
# Every request is scanned, in every environment.
# ============================================================================

def scan_for_secrets(messages: list) -> dict:
    """
    Scan messages for sensitive data (API keys, secrets, PII).
    
    CRITICAL: This function runs ALWAYS - no environment bypass.
    
    Args:
        messages: List of message dicts with 'content' field
        
    Returns:
        dict with:
            - threat_detected: bool
            - threat_type: str (first detected pattern type or 'none')
            - threat_count: int (total matches across all patterns)
            - details: list of detected pattern names
    """
    threat_detected = False
    threat_types = []
    threat_count = 0
    
    # Extract all text content from messages
    all_content = ""
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            all_content += content + " "
        elif isinstance(content, list):
            # Handle multimodal content (text + images)
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    all_content += item.get("text", "") + " "
    
    # Heuristic combo: email + api_key-like token should count as a threat.
    # We deliberately do NOT flag email-only. This is only used when both signals exist.
    email_matches = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", all_content)
    api_key_like_matches = re.findall(r"api_key\s*=\s*[A-Za-z0-9_\-]{8,}", all_content, re.IGNORECASE)
    if email_matches and api_key_like_matches:
        threat_detected = True
        threat_types.extend(["email", "api_key_like"])
        threat_count += len(email_matches) + len(api_key_like_matches)
        log.warning(
            "dlp_combo_email_and_key_detected",
            email_matches=len(email_matches),
            api_key_like_matches=len(api_key_like_matches),
        )

    # Scan for each DLP pattern
    for pattern_name, pattern_regex in DLP_PATTERNS.items():
        try:
            matches = re.findall(pattern_regex, all_content, re.IGNORECASE)
            if matches:
                threat_detected = True
                threat_types.append(pattern_name)
                threat_count += len(matches)
                log.warning(
                    "dlp_threat_detected",
                    pattern=pattern_name,
                    match_count=len(matches),
                    # DO NOT log the actual matches (secret leakage!)
                )
        except re.error as e:
            log.error("dlp_regex_error", pattern=pattern_name, error=str(e)[:100])
    
    return {
        "threat_detected": threat_detected,
        "threat_type": threat_types[0] if threat_types else "none",
        "threat_count": threat_count,
        "details": threat_types
    }


def redact_secrets(content: str) -> str:
    """
    Redact detected secrets from content before logging.
    
    CRITICAL: Use this when logging message content (audit trail).
    """
    redacted = content
    for pattern_name, pattern_regex in DLP_PATTERNS.items():
        try:
            redacted = re.sub(pattern_regex, f"[REDACTED_{pattern_name.upper()}]", redacted, flags=re.IGNORECASE)
        except re.error:
            pass
    return redacted


# ============================================================================
# TOOL/FUNCTION CALL DLP SCANNER (AGENT SUPPORT)
# ============================================================================
# CRITICAL: Agents use tools to interact with external systems.
# Tool parameters and outputs can contain secrets.
# Tool inputs and outputs are scanned too.
# ============================================================================

def _first_threat_type(current: str, found: str) -> str:
    """The type of the first scan that found something.

    A scan that finds nothing hands back the string "none", which is truthy, so
    `current or found` kept "none" and a secret that sat only in `tools` or
    `functions` was refused as "(none)".
    """
    return current if current and current != "none" else found


def scan_tools_for_secrets(tools: list) -> dict:
    """
    Scan tool definitions for sensitive data.
    
    Agents can accidentally expose secrets in:
    - Tool descriptions (e.g., "Use this API key: sk-...")
    - Parameter examples in schemas
    - Default values
    
    Args:
        tools: List of tool/function definitions (OpenAI format)
        
    Returns:
        dict with threat info
    """
    if not tools:
        return {"threat_detected": False, "threat_type": "none", "threat_count": 0, "details": []}
    
    threat_detected = False
    threat_types = []
    threat_count = 0
    
    # Extract all text from tool definitions
    all_content = ""
    for tool in tools:
        # Handle OpenAI format: {"type": "function", "function": {...}}
        if isinstance(tool, dict):
            func = tool.get("function", tool)  # Support both formats
            all_content += func.get("name", "") + " "
            all_content += func.get("description", "") + " "
            # Stringify parameters schema (could contain examples with secrets)
            params = func.get("parameters", {})
            all_content += json.dumps(params) + " "
    
    # Scan for each DLP pattern
    for pattern_name, pattern_regex in DLP_PATTERNS.items():
        try:
            matches = re.findall(pattern_regex, all_content, re.IGNORECASE)
            if matches:
                threat_detected = True
                threat_types.append(f"tool_{pattern_name}")
                threat_count += len(matches)
                log.warning(
                    "dlp_tool_definition_threat",
                    pattern=pattern_name,
                    match_count=len(matches)
                )
        except re.error as e:
            log.error("dlp_tool_regex_error", pattern=pattern_name, error=str(e)[:100])
    
    return {
        "threat_detected": threat_detected,
        "threat_type": threat_types[0] if threat_types else "none",
        "threat_count": threat_count,
        "details": threat_types
    }


def scan_tool_call_for_secrets(tool_call: dict) -> dict:
    """
    Scan a tool call (from LLM response) for sensitive data.
    
    LLMs can generate tool calls with secrets in arguments:
    - {"name": "execute_sql", "arguments": {"query": "SELECT * FROM users WHERE api_key = 'sk-...'"}}
    
    Args:
        tool_call: Tool call object from LLM response
        
    Returns:
        dict with threat info
    """
    if not tool_call:
        return {"threat_detected": False, "threat_type": "none", "threat_count": 0, "details": []}
    
    # Extract arguments as string
    arguments = tool_call.get("function", {}).get("arguments", "")
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    
    return scan_for_secrets([{"content": arguments}])


def scan_content_for_dlp_threats(content: str) -> dict:
    """
    Scan arbitrary content for sensitive data (used in streaming).
    
    Args:
        content: Text content to scan
        
    Returns:
        Dict with threat_detected, threat_type, threat_count, details
    """
    if not content:
        return {"threat_detected": False, "threat_type": "none", "threat_count": 0, "details": []}
    
    threat_detected = False
    threat_types = []
    threat_count = 0
    
    for pattern_name, pattern_regex in DLP_PATTERNS.items():
        try:
            matches = re.findall(pattern_regex, content, re.IGNORECASE)
            if matches:
                threat_detected = True
                threat_types.append(pattern_name)
                threat_count += len(matches)
        except re.error:
            pass
    
    return {
        "threat_detected": threat_detected,
        "threat_type": threat_types[0] if threat_types else "none",
        "threat_count": threat_count,
        "details": threat_types
    }


def scan_tool_result_for_secrets(tool_result: str) -> dict:
    """
    Scan tool execution result for sensitive data.
    
    CRITICAL: Tool outputs (e.g., file contents, DB query results) 
    often contain secrets that should NOT be sent back to LLMs.
    
    Examples:
    - File read: Contains .env with API keys
    - DB query: Returns user passwords
    - API call: Response includes tokens
    
    Args:
        tool_result: String content of tool execution result
        
    Returns:
        dict with threat info
    """
    if not tool_result:
        return {"threat_detected": False, "threat_type": "none", "threat_count": 0, "details": []}
    
    return scan_for_secrets([{"content": tool_result}])


def extract_tool_calls_from_response(response_data: dict) -> list:
    """
    Extract tool_calls from LLM response (OpenAI format).
    
    Handles:
    - OpenAI: choices[0].message.tool_calls
    - Anthropic (converted): choices[0].message.tool_calls
    - Legacy function_call: choices[0].message.function_call
    """
    tool_calls = []
    
    try:
        choices = response_data.get("choices", [])
        if not choices:
            return []
        
        message = choices[0].get("message", {})
        
        # Modern tool_calls format
        if "tool_calls" in message:
            tool_calls = message.get("tool_calls", [])
        
        # Legacy function_call format (convert to tool_calls)
        elif "function_call" in message:
            func_call = message["function_call"]
            tool_calls = [{
                "id": f"call_{int(time.time())}",
                "type": "function",
                "function": func_call
            }]
    
    except Exception as e:
        log.error("extract_tool_calls_failed", error=str(e)[:100])
    
    return tool_calls


# ============================================================================
# AGENT SESSION TRACKING
# ============================================================================
# Track agent "runs" for audit, debugging, and kill switch functionality.
# ============================================================================

# Agent and session ids arrive as request headers: claims, until checked against
# the API key's organization. Ownership is cached briefly in Redis; the kill state
# comes from the Redis kill flag (set at once by every kill path) and from the
# agent_sessions row, which is the authority.
SESSION_CONTEXT_TTL_SECONDS = 30
AGENT_OWNER_TTL_SECONDS = 300


class AgentContextRefused(Exception):
    """A request whose agent or session headers the gateway will not serve."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


async def _session_row_from_db(session_id: str) -> Optional[dict]:
    """The agent_sessions row (owner, agent, kill state), or None. Raises ControlUnavailable."""
    try:
        res = await resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/agent_sessions",
            params={
                "id": f"eq.{session_id}",
                "select": "organization_id,agent_id,status,kill_requested_at",
                "limit": "1",
            },
            headers=get_supabase_headers(),
            timeout=5.0,
        )
        res.raise_for_status()
        rows = res.json() or []
    except Exception as e:
        log_error_safe(log, "session_lookup_failed", e, session_id=session_id)
        raise ControlUnavailable("agent_sessions unreadable") from e
    if not rows:
        return None
    row = rows[0]
    return {
        "organization_id": str(row.get("organization_id")),
        "agent_id": str(row.get("agent_id")),
        "terminated": row.get("status") == "killed" or bool(row.get("kill_requested_at")),
    }


async def _session_context(session_id: str) -> tuple[Optional[dict], bool]:
    """(row, read_from_database_now). Redis copy first, the database on a miss or a Redis error."""
    cache_name = f"oxy:session:{session_id}:ctx"
    if resources.redis is not None:
        try:
            raw = await resources.redis.get(cache_name)
            if raw:
                return json.loads(raw), False
        except Exception as e:
            # Redis cannot answer: the database decides below. Never "assume alive".
            log_error_safe(log, "session_context_cache_failed", e, session_id=session_id)
    row = await _session_row_from_db(session_id)
    if row is not None and resources.redis is not None:
        try:
            await resources.redis.setex(cache_name, SESSION_CONTEXT_TTL_SECONDS, json.dumps(row))
        except Exception:
            pass
    return row, True


async def _agent_owner(agent_id: str) -> Optional[str]:
    """The organization that owns an agent, or None. Raises ControlUnavailable."""
    cache_name = f"oxy:agent:{agent_id}:org"
    if resources.redis is not None:
        try:
            cached = await resources.redis.get(cache_name)
            if cached:
                return cached
        except Exception as e:
            log_error_safe(log, "agent_owner_cache_failed", e, agent_id=agent_id)
    try:
        res = await resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/agents",
            params={"id": f"eq.{agent_id}", "select": "organization_id", "limit": "1"},
            headers=get_supabase_headers(),
            timeout=5.0,
        )
        res.raise_for_status()
        rows = res.json() or []
    except Exception as e:
        log_error_safe(log, "agent_lookup_failed", e, agent_id=agent_id)
        raise ControlUnavailable("agents unreadable") from e
    if not rows:
        return None
    owner = str(rows[0].get("organization_id"))
    if resources.redis is not None:
        try:
            await resources.redis.setex(cache_name, AGENT_OWNER_TTL_SECONDS, owner)
        except Exception:
            pass
    return owner


async def resolve_agent_context(request_headers: dict, organization_id: str) -> dict:
    """
    Agent and session of a request, checked against the API key's organization.

    Headers:
    - X-Oxy-Agent-Id: agent UUID (optional)
    - X-Oxy-Session-Id: agent session UUID (optional; the agent is then the session's)
    - X-Oxy-Agent-Name: human-readable agent name (for logging)

    An agent or session the key's organization does not own is refused with 403,
    the same answer as for an id that does not exist, so one tenant cannot probe
    another's ids or write them into its own request_logs rows. A killed session
    is refused with 410. If neither Redis nor the database can say whether the
    session was killed, ControlUnavailable is raised and the request is refused:
    the kill switch fails closed.

    Returns dict with agent_id, session_id, agent_name (all None for a plain request).
    """
    agent_id = request_headers.get("x-oxy-agent-id") or None
    session_id = request_headers.get("x-oxy-session-id") or None
    agent_name = request_headers.get("x-oxy-agent-name", "unknown")

    if not agent_id and not session_id:
        return {"agent_id": None, "session_id": None, "agent_name": None}

    for header, value in (("X-Oxy-Agent-Id", agent_id), ("X-Oxy-Session-Id", session_id)):
        if value and not _is_uuid(value):
            raise AgentContextRefused(400, "invalid_agent_header", f"{header} must be a UUID.")

    if session_id:
        row, fresh = await _session_context(session_id)
        if row is None or row.get("organization_id") != str(organization_id):
            raise AgentContextRefused(
                403, "session_not_in_organization",
                "This session does not belong to the API key's organization.",
            )
        if agent_id and row.get("agent_id") != str(agent_id):
            raise AgentContextRefused(
                403, "session_not_of_agent",
                "This session belongs to a different agent.",
            )
        agent_id = row.get("agent_id")
        terminated = bool(row.get("terminated"))
        if not terminated:
            try:
                terminated = await is_session_terminated(session_id)
            except ControlUnavailable:
                if not fresh:
                    # The copy came from Redis, which has stopped answering: ask the database.
                    fresh_row = await _session_row_from_db(session_id)
                    terminated = fresh_row is None or bool(fresh_row.get("terminated"))
                # A row read from the database just now already decides.
        if terminated:
            log.warning("agent_session_killed", session_id=session_id)
            raise AgentContextRefused(
                410, "session_terminated",
                f"Agent session {session_id} has been terminated. Create a new session with "
                "POST /v1/agents/{agent_id}/sessions; do not retry with this session_id.",
            )
    else:
        owner = await _agent_owner(agent_id)
        if owner != str(organization_id):
            raise AgentContextRefused(
                403, "agent_not_in_organization",
                "This agent does not belong to the API key's organization.",
            )

    return {"agent_id": agent_id, "session_id": session_id, "agent_name": agent_name}


async def log_tool_call(
    organization_id: str,
    session_id: str,
    tool_name: str,
    tool_id: str,
    input_parameters: dict,
    output_result: any,
    threat_detected: bool = False,
    threat_type: str = None,
    was_blocked: bool = False,
    was_redacted: bool = False,
    duration_ms: int = None,
    status: str = "success",
    error_message: str = None
):
    """
    Log a tool call for audit trail.
    
    Stored in tool_call_logs table for:
    - Security auditing
    - Debugging
    - Compliance
    - Usage analytics
    """
    try:
        await resources.supabase.table("tool_call_logs").insert({
            "organization_id": organization_id,
            "agent_session_id": session_id,
            "tool_name": tool_name,
            "tool_id": tool_id,
            "input_parameters": input_parameters,
            "output_result": output_result if not was_redacted else {"redacted": True},
            "threat_detected": threat_detected,
            "threat_type": threat_type,
            "was_blocked": was_blocked,
            "was_redacted": was_redacted,
            "duration_ms": duration_ms,
            "status": status,
            "error_message": error_message
        }).execute()
    except Exception as e:
        log.error("log_tool_call_failed", tool_name=tool_name, error=str(e)[:200])


def maybe_handle_gateway_healthcheck(model: str, messages: list[dict], provider_kwargs: dict):
    """
    Short-circuit healthcheck for the Oxy Gateway.
    Triggered only when:
      - model is gpt-4.1-mini
      - system message contains "healthcheck bot for the Oxy Gateway"
      - user message content is exactly "ping"
    Returns JSONResponse or None to continue normal flow.
    """
    try:
        if model != "gpt-4.1-mini":
            return None
        system_ok = any(
            m.get("role") == "system"
            and "healthcheck bot for the oxy gateway" in str(m.get("content", "")).lower()
            for m in messages
        )
        user_ok = any(m.get("role") == "user" and m.get("content") == "ping" for m in messages)
        if not (system_ok and user_ok):
            return None
        response = {
            "id": "chatcmpl-healthcheck",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "pong from Oxy Gateway"
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }
        return JSONResponse(response)
    except Exception:
        return None


# ============================================================================
# PROXY ENDPOINT
# ============================================================================

@router.post("/chat/completions")
@limiter.limit(f"{RATE_LIMIT_PER_MINUTE}/minute", key_func=get_rate_limit_key)
async def chat_completions_proxy(
    request: Request,
    bt: BackgroundTasks,
    key_data: CacheData = Depends(api_key_dependency),
    creds: HTTPAuthorizationCredentials = Depends(auth_scheme)
):
    """
    Universal AI Gateway Proxy - Routes to OpenAI/Anthropic/Google.
    
    This is the main request path. Handle with extreme care.
    
    Flow:
    1. Extract API key metadata (user_id, project_id, budget_limit)
    2. Burst rate limiting (10 req/sec)
    3. Organization suspension
    4. Agent and session checked against the key's organization; kill switch
    5. DLP, then budget reservation (atomic Redis Lua; refused when it cannot be checked)
    6. Parse request body
    7. Route to appropriate provider
    8. Stream/return response
    9. Log request + adjust budget
    """
    
    # Extract key metadata
    user_id = key_data['user_id']
    project_id = key_data['project_id']
    organization_id = key_data['organization_id']
    oxy_api_key = key_data['api_key']  # The Oxy API key used for auth
    budget_limit = key_data['budget_limit']
    api_key_used = creds.credentials
    # Start timer for latency_ms recording — read at each bt.add_task call site below.
    request_start_ms = time.monotonic()
    # The key was used, refused or not: the API Keys page shows when.
    note_key_use(key_data.get("key_id"))

    # Load org plan/flags if missing in cache
    if key_data.get("plan_tier") is None or key_data.get("org_paused") is None:
        if getattr(resources, "http_client", None) is None:
            key_data.setdefault("plan_tier", "free")
            key_data.setdefault("org_paused", False)
            key_data.setdefault("auto_suspend_on_cost_spikes", True)
        else:
            org_meta = await _load_org_plan_and_flags(organization_id)
            key_data["plan_tier"] = org_meta.get("plan_tier", "free")
            key_data["org_paused"] = org_meta.get("org_paused", False)
            key_data["auto_suspend_on_cost_spikes"] = org_meta.get("auto_suspend_on_cost_spikes", True)
    if key_data.get("privacy_mode") is None:
        key_data["privacy_mode"] = await _load_org_privacy_mode(organization_id)

    org_status_payload = await get_org_status(organization_id)
    if org_status_payload.get("status") == "suspended":
        await record_blocked_request(
            key_data=key_data, model=await _model_of(request), messages=[], reason="org_suspended",
            request_start_ms=request_start_ms,
        )
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "org_suspended",
                    "message": "This organization is suspended and cannot accept new requests.",
                    "reason": org_status_payload.get("reason"),
                }
            },
        )
    org_paused = bool(key_data.get("org_paused", False) or org_status_payload.get("status") == "suspended")

    # No plan request quota on the request path: a self-hosted gateway does not cap
    # an organization's requests by a plan tier. Budgets (below) are the spend
    # control; monthly usage is still counted for the dashboard.

    # Burst rate limiting. A refused request still gets its audit row: the
    # prompt is not stored (it was never read), the model is when the body has one.
    try:
        await check_burst_rate_limit(request, user_id)
    except HTTPException as exc:
        if exc.status_code == 429:
            await record_blocked_request(
                key_data=key_data, model=await _model_of(request), messages=[], reason="rate_limited",
                request_start_ms=request_start_ms,
            )
        raise
    
    # No subscription lookup on the request path; request logs carry no subscription.
    subscription_id = None

    # Parse request body
    try:
        body_bytes = await request.body()
        body_dict = json.loads(body_bytes)
        model = body_dict.get("model", "gpt-4")
        messages = body_dict.get("messages", [])
        provider_kwargs = {k: v for k, v in body_dict.items() if k not in ("model", "messages")}
        prompt_snapshot = json.dumps(messages)
        
        # 🆕 AGENT SUPPORT: Extract tool/function definitions
        tools = body_dict.get("tools", [])
        tool_choice = body_dict.get("tool_choice")
        functions = body_dict.get("functions", [])  # Legacy format
        function_call = body_dict.get("function_call")  # Legacy format
        
    except Exception as e:
        await record_blocked_request(
            key_data=key_data, model="unknown", messages=[], reason="invalid_request",
            request_start_ms=request_start_ms,
        )
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    # Short-circuit healthcheck (no provider call). Answered by the gateway itself,
    # and audited like any other request.
    healthcheck_response = maybe_handle_gateway_healthcheck(model, messages, provider_kwargs)
    if healthcheck_response:
        await record_request(
            user_id=user_id, project_id=project_id, organization_id=organization_id,
            api_key_id=key_data.get("key_id"), model=model, provider="oxy-healthcheck",
            status="success", threat="none", threat_type="none", prompt_tokens=0,
            completion_tokens=0, cost=Decimal("0"), subscription_id=None,
            reserved_amount=Decimal("0"), cache_hit=False,
            privacy_mode=key_data.get("privacy_mode") or DEFAULT_MODE,
            prompt_text=json.dumps(messages), response_text=None,
            latency_ms=int((time.monotonic() - request_start_ms) * 1000),
        )
        return healthcheck_response
    
    provider_name = provider_registry.get_provider_name_for_model(model)
    
    # =========================================================================
    # 🆕 AGENT SESSION TRACKING
    # =========================================================================
    # Check for agent headers and validate session
    # =========================================================================
    try:
        agent_context = await resolve_agent_context(dict(request.headers), organization_id)
    except (AgentContextRefused, ControlUnavailable) as e:
        if isinstance(e, AgentContextRefused):
            status, code, message = e.status_code, e.code, e.message
        else:
            status, code = 503, "kill_switch_unavailable"
            message = "The kill switch cannot be checked right now; the request is refused."
        await record_blocked_request(
            key_data=key_data, model=model, messages=messages, reason=code,
            request_start_ms=request_start_ms,
            agent_id=request.headers.get("x-oxy-agent-id"), session_id=request.headers.get("x-oxy-session-id"),
        )
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})
    agent_id = agent_context.get("agent_id")
    session_id = agent_context.get("session_id")
    agent_name = agent_context.get("agent_name")
    privacy_mode = key_data.get("privacy_mode") or DEFAULT_MODE
    
    if agent_id:
        log.info("agent_request", 
                agent_id=agent_id, 
                session_id=session_id, 
                agent_name=agent_name,
                has_tools=len(tools) > 0)

    # =========================================================================
    # CENTRAL CONTROL HOOK (org/agent/session status)
    # =========================================================================
    control_result = await check_execution_allowed(
        org_id=organization_id,
        agent_id=agent_id,
        session_id=None,  # checked above by resolve_agent_context, with the database as authority
    )
    if not control_result.get("allowed", False):
        await record_blocked_request(
            key_data=key_data, model=model, messages=messages,
            reason=control_result.get("code", "blocked"), request_start_ms=request_start_ms,
            agent_id=agent_id, session_id=session_id,
        )
        error_payload = {
            "error": {
                "code": control_result.get("code", "blocked"),
                "message": control_result.get("message", "Request blocked by control policy."),
            }
        }
        if control_result.get("reason"):
            error_payload["error"]["reason"] = control_result.get("reason")
        if control_result.get("suspended_until"):
            error_payload["error"]["suspended_until"] = control_result.get("suspended_until")
        return JSONResponse(
            status_code=control_result.get("status_code", 403),
            content=error_payload,
        )
    # Sync org_paused flag from control (if present)
    org_paused = org_paused or control_result.get("org_status") in ("suspended", "disabled", "paused")
    
    # =========================================================================
    # DLP SCANNING (ALWAYS ENABLED - NO DEV MODE BYPASS)
    # =========================================================================
    # CRITICAL: Scan for secrets BEFORE any processing.
    # This runs in ALL environments: localhost, staging, production.
    # v0.1 scope: secrets/keys/PCI only (no broad PII). Messages/tools are
    # hard-blocked here; tool call arguments are handled later via log+redact.
    # =========================================================================
    
    # 1. Scan messages
    dlp_mode = key_data.get("dlp_mode", "block")
    dlp_result = scan_for_secrets(messages)
    threat_detected = dlp_result["threat_detected"]
    threat_type = dlp_result["threat_type"]
    threat_count = dlp_result["threat_count"]
    all_threats = dlp_result["details"].copy()
    
    # 2. 🆕 Scan tool definitions (hard block if secrets/keys/PCI found)
    if tools:
        tool_dlp_result = scan_tools_for_secrets(tools)
        if tool_dlp_result["threat_detected"]:
            threat_detected = True
            threat_type = _first_threat_type(threat_type, tool_dlp_result["threat_type"])
            threat_count += tool_dlp_result["threat_count"]
            all_threats.extend(tool_dlp_result["details"])
            log.warning("dlp_tool_definition_blocked",
                       user_id=user_id,
                       threats=tool_dlp_result["details"])
    
    # 3. 🆕 Scan legacy functions (convert to tools format)
    if functions:
        legacy_tools = [{"type": "function", "function": f} for f in functions]
        func_dlp_result = scan_tools_for_secrets(legacy_tools)
        if func_dlp_result["threat_detected"]:
            threat_detected = True
            threat_type = _first_threat_type(threat_type, func_dlp_result["threat_type"])
            threat_count += func_dlp_result["threat_count"]
            all_threats.extend(func_dlp_result["details"])
    
    if threat_detected:
        log.warning(
            "dlp_threat_detected",
            user_id=user_id,
            project_id=project_id,
            organization_id=organization_id,
            threat_type=threat_type,
            threat_count=threat_count,
            patterns_detected=all_threats,
            mode=dlp_mode,
        )
        # Log the event for audit trail (even in log-only mode)
        bt.add_task(
            record_request,
            user_id=user_id,
            project_id=project_id,
            organization_id=organization_id,
            api_key_id=key_data.get("key_id"),
            agent_id=agent_id,
            session_id=session_id,
            model=model,
            provider="blocked" if dlp_mode == "block" else provider_name,
            status="blocked" if dlp_mode == "block" else "success",
            threat="blocked" if dlp_mode == "block" else "detected",
            threat_type=threat_type,
            prompt_tokens=0,
            completion_tokens=0,
            cost=Decimal("0"),
            subscription_id=subscription_id,
            reserved_amount=Decimal("0"),
            cache_hit=False,
            cost_tags={"agent_id": agent_id, "project_id": project_id} if agent_id else {"project_id": project_id},
            privacy_mode=privacy_mode,
            prompt_text=json.dumps(messages),
            response_text=None,
            latency_ms=int((time.monotonic() - request_start_ms) * 1000),
        )

        if dlp_mode == "block":
            return JSONResponse(
                status_code=451,
                content={
                    "error": {
                        "code": "DLP_BLOCKED",
                        "message": f"Request blocked: Sensitive data detected ({threat_type}).",
                        "hint": f"Remove the {threat_type} from your message. The gateway checks for API keys, tokens, private keys and card numbers.",
                    }
                },
            )
    
    # Budget reservation. Fails closed: a budget that cannot be checked is not
    # spent, the request is refused with 503 and recorded.
    reservation_amount = Decimal("0")
    if budget_limit > 0:
        if getattr(resources, "budget_manager", None) is None:
            # Only in offline tests: the engine does not start without Redis.
            log.debug("budget_manager_unavailable_skip_reservation", project_id=project_id)
        else:
            try:
                reservation_amount = estimate_max_cost_for_model(model, len(messages))
                
                result = await resources.budget_manager.reserve_budget(
                    project_id=project_id,
                    organization_id=organization_id,
                    amount=reservation_amount,
                    budget_limit=Decimal(str(budget_limit))
                )
                status = str(result.get("status", ""))
                
                if status == "BUDGET_EXCEEDED":
                    await record_blocked_request(
                        key_data=key_data, model=model, messages=messages, reason="budget_exceeded",
                        request_start_ms=request_start_ms, agent_id=agent_id, session_id=session_id,
                    )
                    raise ai_error(
                        status_code=402,  # Payment Required
                        code="BUDGET_EXCEEDED",
                        message=f"Budget limit of ${budget_limit} exceeded.",
                        hint="Ask an operator to raise the project's budget_limit. Do not retry until it changes.",
                        context={"budget_limit": float(budget_limit), "current_spend": float(result.get("current_spend", 0))}
                    )
                if not status.startswith("OK"):
                    raise RuntimeError(f"budget reservation returned {status or 'nothing'}")
                if status in ("OK", "OK_UNLIMITED"):
                    from workers.budget_alerts import check_budget_and_alert
                    bt.add_task(
                        check_budget_and_alert,
                        project_id,
                        result.get("current_spend", Decimal("0")),
                        Decimal(str(budget_limit)),
                        user_id,
                        organization_id,
                    )
            except HTTPException:
                raise
            except Exception as e:
                log.error(
                    "budget_check_failed",
                    user_id=user_id,
                    project_id=project_id,
                    organization_id=organization_id,
                    budget_limit=str(budget_limit),
                    reservation_amount=str(reservation_amount),
                    error_type=type(e).__name__,
                    error_message=str(e)[:200],
                )
                await record_blocked_request(
                    key_data=key_data, model=model, messages=messages, reason="budget_unavailable",
                    request_start_ms=request_start_ms, agent_id=agent_id, session_id=session_id,
                )
                return JSONResponse(
                    status_code=503,
                    content={"error": {
                        "code": "budget_unavailable",
                        "message": "The budget cannot be checked right now; the request is refused.",
                    }},
                    headers={"Retry-After": "5"},
                )
    
    # --- OXY MOCK INTERCEPTOR (E2E Testing) ---
    # Allows testing Budget & DLP without external API calls.
    # Triggered ONLY if model is 'oxy-mock'.
    # ORDER: DLP → Budget → THIS → Provider (never reaches provider)
    if model == "oxy-mock":
        # Simulate network latency to allow race conditions to manifest
        # This gives concurrent requests time to overlap in the budget check window
        await asyncio.sleep(0.05)
        
        log.info("oxy_mock_hit", project_id=project_id, user_id=user_id, cost="reserved")
        return JSONResponse({
            "id": f"mock-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "oxy-mock",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "🟢 SYSTEM OPERATIONAL."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}
        })
    # ------------------------------------------
    
    # Route to provider
    # Step 1: Determine which provider handles this model (without instantiating)
    provider_name = provider_registry.get_provider_name_for_model(model)
    
    # =========================================================================
    # DEV MODE BYPASS REMOVED (2025-11-27)
    # =========================================================================
    # SECURITY FIX: The old "dev_mode_nuclear_bypass" skipped DLP scanning.
    # This was a security vulnerability - secrets could be leaked in dev mode.
    # 
    # Now ALL requests go through full security pipeline:
    # 1. DLP scanning (above) - ALWAYS
    # 2. Budget checks - ALWAYS
    # 3. Provider routing - ALWAYS
    #
    # For development without external API calls, use:
    # - Mock providers in provider_registry
    # - Integration tests with mocked responses
    # =========================================================================
    
    # Step 2: Fetch the provider's API key from DB/Vault
    provider_api_key = await get_provider_api_key(organization_id, provider_name)
    
    # =========================================================================
    # UNIVERSAL MOCK MODE (All Providers)
    # =========================================================================
    # Supports test keys for ALL providers:
    #   - "sk-test-*"      → OpenAI test keys
    #   - "sk-ant-test-*"  → Anthropic test keys  
    #   - "AIza-test-*"    → Google Gemini test keys
    #   - "mock-*"         → Generic mock (any provider)
    #
    # SECURITY: DLP scanning already ran above - this is SAFE.
    # USAGE: Store any test key in Settings to enable mock responses.
    # =========================================================================
    MOCK_KEY_PREFIXES = [
        "sk-test",       # OpenAI
        "sk-ant-test",   # Anthropic
        "AIza-test",     # Google Gemini
        "mock-",         # Generic (works for any provider)
    ]
    
    # Check if provider key matches any mock prefix
    matched_prefix = None
    if provider_api_key:
        for prefix in MOCK_KEY_PREFIXES:
            if provider_api_key.startswith(prefix):
                matched_prefix = prefix
                break
    
    if matched_prefix:
        log.info(
            "universal_mock_hit",
            user_id=user_id,
            project_id=project_id,
            provider=provider_name,
            model=model,
            key_prefix=matched_prefix
        )
        
        # Calculate mock cost for budget tracking
        mock_prompt_tokens = 10
        mock_completion_tokens = 5
        mock_cost = PricingEngine.calculate_cost(model, mock_prompt_tokens, mock_completion_tokens)
        
        # Log the mock request for audit trail
        bt.add_task(
            log_request,
            user_id, project_id, organization_id, model, provider_name,
            "mock", "safe", "none",
            mock_prompt_tokens, mock_completion_tokens, mock_cost,
            subscription_id, reservation_amount,
            api_key_id=key_data.get("key_id"), agent_id=agent_id, session_id=session_id,
            latency_ms=int((time.monotonic() - request_start_ms) * 1000),
        )
        
        # Return OpenAI-compatible response (universal format for all providers)
        return JSONResponse({
            "id": f"chatcmpl-mock-{provider_name}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,  # Returns requested model (e.g., "claude-3-opus", "gemini-1.5-pro")
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": (
                        f"🧪 Universal Mock Response\n\n"
                        f"Provider: {provider_name}\n"
                        f"Model: {model}\n"
                        f"Key Prefix: {matched_prefix}\n\n"
                        f"This is a test response. No external API was called."
                    )
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": mock_prompt_tokens,
                "completion_tokens": mock_completion_tokens,
                "total_tokens": mock_prompt_tokens + mock_completion_tokens
            }
        })
    # =========================================================================
    
    # Step 3: Get provider instance with the correct API key and http_client
    provider_instance = provider_registry.get_provider_for_model(
        model=model,
        api_key=provider_api_key,
        http_client=resources.http_client
    )
    
    stream = body_dict.get("stream", False)
    response_cache = resources.response_cache
    cached_response_data = None
    
    if not stream and response_cache:
        try:
            cached_response_data = await response_cache.get_cached_response(
                provider=provider_name,
                model=model,
                messages=messages,
                params=body_dict,
                scope=organization_id,
            )
        except Exception as cache_error:
            log.warning(
                "response_cache_get_failed",
                provider=provider_name,
                model=model,
                project_id=project_id,
                error=str(cache_error)[:200]
            )
    
    if cached_response_data:
        usage = cached_response_data.get("usage", {}) or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        cost = calculate_cost(model, prompt_tokens, completion_tokens)
        
        bt.add_task(
            record_request,
            user_id=user_id,
            project_id=project_id,
            organization_id=organization_id,
            api_key_id=key_data.get("key_id"),
            agent_id=agent_id,
            session_id=session_id,
            model=model,
            provider=provider_name,
            status="cache_hit",
            threat="safe",
            threat_type="none",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            subscription_id=subscription_id,
            reserved_amount=reservation_amount,
            cache_hit=True,
            cost_tags={"agent_id": agent_id, "project_id": project_id} if agent_id else {"project_id": project_id},
            privacy_mode=privacy_mode,
            prompt_text=json.dumps(messages),
            response_text=json.dumps(cached_response_data),
            latency_ms=int((time.monotonic() - request_start_ms) * 1000),
        )
        bt.add_task(check_and_celebrate_first_request, user_id, cost, model)
        
        log.info(
            "response_cache_hit",
            provider=provider_name,
            model=model,
            project_id=project_id
        )
        
        return Response(
            content=json.dumps(cached_response_data),
            media_type="application/json",
            headers={"X-Oxy-Cache": "hit"}
        )
    
    # Make provider request
    try:
        if stream:
            # =================================================================
            # STREAMING IMPLEMENTATION
            # =================================================================
            # Provider.stream() returns AsyncGenerator[str, None] of SSE events
            # We wrap it with DLP scanning, usage tracking, and proper cleanup
            # =================================================================
            
            async def stream_with_tracking():
                """
                Streaming response with:
                1. Real-time token accumulation
                2. DLP scanning on accumulated content
                3. Proper usage logging on completion
                4. Session tracking for agents
                """
                accumulated_content = ""
                accumulated_tool_calls = []
                prompt_tokens = 0
                completion_tokens = 0
                
                try:
                    # Stream from provider
                    async for chunk in provider_instance.stream(
                        model=model, 
                        messages=messages, 
                        **provider_kwargs
                    ):
                        # Yield SSE chunk directly
                        yield chunk
                        
                        # Cooperative cancellation (kill switch). A flag that cannot
                        # be read stops the stream too: the kill switch fails closed.
                        if session_id:
                            try:
                                stop_stream = await is_session_terminated(session_id)
                            except ControlUnavailable:
                                stop_stream = True
                            if stop_stream:
                                log.info("stream_terminated_by_flag", session_id=session_id)
                                yield "data: [DONE]\n\n"
                                return
                        
                        # Parse chunk for token tracking (best effort)
                        if chunk.startswith("data: ") and chunk != "data: [DONE]\n\n":
                            try:
                                chunk_json = json.loads(chunk[6:].strip())
                                choices = chunk_json.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        accumulated_content += content
                                    
                                    # Track tool calls
                                    tool_call_delta = delta.get("tool_calls", [])
                                    if tool_call_delta:
                                        accumulated_tool_calls.extend(tool_call_delta)
                                
                                # Check for usage in final chunk
                                usage = chunk_json.get("usage", {})
                                if usage:
                                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                                    completion_tokens = usage.get("completion_tokens", completion_tokens)
                            except json.JSONDecodeError:
                                pass  # Skip malformed chunks
                    
                    # Estimate tokens if not provided
                    if completion_tokens == 0 and accumulated_content:
                        completion_tokens = max(1, len(accumulated_content) // 4)
                    if prompt_tokens == 0:
                        total_input = sum(len(str(m.get("content", ""))) for m in messages)
                        prompt_tokens = max(1, total_input // 4)
                    
                    # Calculate cost
                    cost = calculate_cost(model, prompt_tokens, completion_tokens)
                    
                    # DLP scan accumulated content (async background)
                    content_scan = scan_content_for_dlp_threats(accumulated_content)
                    threat_type = "none"
                    if content_scan["threat_detected"]:
                        threat_type = content_scan["threat_type"]
                        log.warning("dlp_stream_threat_detected",
                                   model=model,
                                   threats=content_scan["details"],
                                   session_id=session_id)
                    
                    # Log request (background)
                    bt.add_task(
                        record_request,
                        user_id=user_id,
                        project_id=project_id,
                        organization_id=organization_id,
                        api_key_id=key_data.get("key_id"),
                        agent_id=agent_id,
                        session_id=session_id,
                        model=model,
                        provider=provider_name,
                        status="success",
                        threat="safe" if threat_type == "none" else "stream_threat",
                        threat_type=threat_type,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cost=cost,
                        subscription_id=subscription_id,
                        reserved_amount=reservation_amount,
                        cache_hit=False,
                        cost_tags={"agent_id": agent_id, "project_id": project_id} if agent_id else {"project_id": project_id},
                        privacy_mode=privacy_mode,
                        prompt_text=json.dumps(messages),
                        response_text=accumulated_content,
                        latency_ms=int((time.monotonic() - request_start_ms) * 1000),
                    )
                    
                    # First request ceremony
                    bt.add_task(check_and_celebrate_first_request, user_id, cost, model)
                    
                    log.info("stream_complete",
                            model=model,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            cost=float(cost))
                    
                except Exception as stream_error:
                    log.error("stream_failed", 
                             model=model, 
                             error=str(stream_error)[:200])
                    # Yield error as SSE event
                    error_event = {
                        "error": {
                            "message": "Stream interrupted",
                            "type": "stream_error",
                            "code": "stream_interrupted"
                        }
                    }
                    yield f"data: {json.dumps(error_event)}\n\n"
                    yield "data: [DONE]\n\n"
                    
                    # Log error
                    bt.add_task(
                        record_request,
                        user_id=user_id,
                        project_id=project_id,
                        organization_id=organization_id,
                        api_key_id=key_data.get("key_id"),
                        agent_id=agent_id,
                        session_id=session_id,
                        model=model,
                        provider=provider_name,
                        status="error",
                        threat="safe",
                        threat_type="none",
                        prompt_tokens=0,
                        completion_tokens=0,
                        cost=Decimal("0"),
                        subscription_id=subscription_id,
                        reserved_amount=reservation_amount,
                        cache_hit=False,
                        cost_tags={"agent_id": agent_id, "project_id": project_id} if agent_id else {"project_id": project_id},
                        privacy_mode=privacy_mode,
                        prompt_text=json.dumps(messages),
                        response_text=None,
                        latency_ms=int((time.monotonic() - request_start_ms) * 1000),
                    )
            
            # Return streaming response
            try:
                await increment_org_monthly_usage(organization_id)
            except Exception:
                log.warn("usage_increment_failed_soft", organization_id=organization_id)
            return StreamingResponse(
                stream_with_tracking(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Oxy-Stream": "true",
                    "X-Oxy-Model": model,
                    "X-Oxy-Session-Id": session_id or "",
                }
            )
        else:
            response_data = await provider_instance.complete(model=model, messages=messages, **provider_kwargs)
            try:
                await increment_org_monthly_usage(organization_id)
            except Exception:
                log.warn("usage_increment_failed_soft", organization_id=organization_id)
            
            # Extract usage
            usage = response_data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            cost = calculate_cost(model, prompt_tokens, completion_tokens)
            
            # =================================================================
            # 🆕 TOOL CALL DLP SCANNING (AGENT SECURITY)
            # =================================================================
            # Scan tool calls in LLM response for secrets.
            # LLMs can generate malicious tool calls with exposed secrets.
            # =================================================================
            tool_calls = extract_tool_calls_from_response(response_data)
            tool_call_threats = []
            
            if tool_calls:
                for tc in tool_calls:
                    tc_dlp = scan_tool_call_for_secrets(tc)
                    if tc_dlp["threat_detected"]:
                        tool_call_threats.extend(tc_dlp["details"])
                        log.warning("dlp_tool_call_threat",
                                   tool_name=tc.get("function", {}).get("name"),
                                   threats=tc_dlp["details"],
                                   session_id=session_id)
                        
                        # Log the tool call for audit
                        if session_id:
                            bt.add_task(
                                log_tool_call,
                                organization_id=organization_id,
                                session_id=session_id,
                                tool_name=tc.get("function", {}).get("name", "unknown"),
                                tool_id=None,
                                input_parameters=tc.get("function", {}).get("arguments", {}),
                                output_result=None,
                                threat_detected=True,
                                threat_type=tc_dlp["threat_type"],
                                was_blocked=False,  # We don't block, just log
                                status="threat_detected"
                            )
                
                # Optionally redact secrets from tool call arguments
                if tool_call_threats:
                    # Redact secrets in the response before returning
                    for i, choice in enumerate(response_data.get("choices", [])):
                        message = choice.get("message", {})
                        if "tool_calls" in message:
                            for j, tc in enumerate(message["tool_calls"]):
                                args = tc.get("function", {}).get("arguments", "")
                                if isinstance(args, str):
                                    response_data["choices"][i]["message"]["tool_calls"][j]["function"]["arguments"] = redact_secrets(args)
            
            response_text = json.dumps(response_data)

            if response_cache:
                try:
                    await response_cache.set_cached_response(
                        provider=provider_name,
                        model=model,
                        messages=messages,
                        params=body_dict,
                        response=response_data,
                        estimated_cost=float(cost),
                        scope=organization_id,
                    )
                except Exception as cache_error:
                    log.warning(
                        "response_cache_set_failed",
                        provider=provider_name,
                        model=model,
                        project_id=project_id,
                        error=str(cache_error)[:200]
                    )
            
            # Log request (include tool info)
            log_status = "success"
            log_threat = "safe" if not tool_call_threats else "tool_threat"
            log_threat_type = "none" if not tool_call_threats else ",".join(tool_call_threats[:3])
            
            bt.add_task(
                log_request,
                user_id, project_id, organization_id, model, provider_name,
                log_status, log_threat, log_threat_type, 
                prompt_tokens, completion_tokens, cost, subscription_id, reservation_amount,
                api_key_id=key_data.get("key_id"), agent_id=agent_id, session_id=session_id,
                privacy_mode=privacy_mode,
                prompt_text=prompt_snapshot,
                response_text=response_text,
                latency_ms=int((time.monotonic() - request_start_ms) * 1000),
            )
            
            # ====================================================================
            # FIRST REQUEST ACHIEVEMENT
            # ====================================================================
            # Check if this is user's first request (0 previous request_logs)
            # If so: Set X-Oxy-First-Request header + create achievement record
            # Frontend detects header → triggers confetti celebration modal
            # ====================================================================
            bt.add_task(check_and_celebrate_first_request, user_id, cost, model)
            
            # Add tool call metadata to response headers
            headers = {}
            if tool_calls:
                headers["X-Oxy-Tool-Calls"] = str(len(tool_calls))
            if session_id:
                headers["X-Oxy-Session-Id"] = session_id
            if tool_call_threats:
                headers["X-Oxy-Tool-Threats"] = ",".join(tool_call_threats[:5])
            
            return Response(
                content=response_text, 
                media_type="application/json",
                headers=headers if headers else None
            )
    
    except ValueError as e:
        log.error("provider_request_config_error", user_id=user_id, error=str(e))
        bt.add_task(
            log_request,
            user_id, project_id, organization_id, model, provider_name,
            "error", "safe", "none", 0, 0, Decimal("0"), subscription_id, reservation_amount,
            api_key_id=key_data.get("key_id"), agent_id=agent_id, session_id=session_id,
            privacy_mode=privacy_mode,
            prompt_text=prompt_snapshot,
            response_text=None,
            latency_ms=int((time.monotonic() - request_start_ms) * 1000),
        )
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "PROVIDER_CONFIG_MISSING",
                    "message": str(e),
                    "hint": "Set OPENAI_API_KEY or configure a provider key for this organization."
                }
            }
        )
    except ProviderHTTPError as e:
        status_code = e.status_code or 502
        log.error(
            "provider_http_error_structured",
            user_id=user_id,
            provider=e.provider,
            status_code=status_code,
            error_body=e.safe_body
        )
        bt.add_task(
            log_request,
            user_id, project_id, organization_id, model, provider_name,
            "error", "safe", "none", 0, 0, Decimal("0"), subscription_id, reservation_amount,
            api_key_id=key_data.get("key_id"), agent_id=agent_id, session_id=session_id,
            privacy_mode=privacy_mode,
            prompt_text=prompt_snapshot,
            response_text=None,
            latency_ms=int((time.monotonic() - request_start_ms) * 1000),
        )
        raise HTTPException(
            status_code=status_code,
            detail={
                "error": {
                    "code": "PROVIDER_HTTP_ERROR",
                    "message": e.message,
                    "provider": e.provider
                }
            }
        )
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code if e.response else 502
        safe_body = log_error_safe(e.response.text)[:500] if e.response else "no_response_body"
        log.error(
            "provider_http_error",
            user_id=user_id,
            status_code=status_code,
            error_body=safe_body,
            provider=provider_name
        )
        bt.add_task(
            log_request,
            user_id, project_id, organization_id, model, provider_name,
            "error", "safe", "none", 0, 0, Decimal("0"), subscription_id, reservation_amount,
            api_key_id=key_data.get("key_id"), agent_id=agent_id, session_id=session_id,
            privacy_mode=privacy_mode,
            prompt_text=prompt_snapshot,
            response_text=None,
            latency_ms=int((time.monotonic() - request_start_ms) * 1000),
        )
        raise HTTPException(
            status_code=status_code,
            detail={
                "error": {
                    "code": "PROVIDER_HTTP_ERROR",
                    "message": f"{provider_name} returned HTTP {status_code}",
                    "provider": provider_name
                }
            }
        )
    except httpx.RequestError as e:
        log.error(
            "provider_unreachable",
            user_id=user_id,
            provider=provider_name,
            error=str(e)
        )
        bt.add_task(
            log_request,
            user_id, project_id, organization_id, model, provider_name,
            "error", "safe", "none", 0, 0, Decimal("0"), subscription_id, reservation_amount,
            api_key_id=key_data.get("key_id"), agent_id=agent_id, session_id=session_id,
            latency_ms=int((time.monotonic() - request_start_ms) * 1000),
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "code": "PROVIDER_UNREACHABLE",
                    "message": f"Failed to reach {provider_name} endpoint.",
                    "provider": provider_name,
                    "hint": "Check OPENAI_API_KEY/OPENAI_API_BASE and network connectivity."
                }
            }
        )
    except Exception as e:
        log.error("provider_request_failed", user_id=user_id, error=str(e), exc_info=True)
        # Log error
        bt.add_task(
            log_request,
            user_id, project_id, organization_id, model, provider_name,
            "error", "safe", "none", 0, 0, Decimal("0"), subscription_id, reservation_amount,
            api_key_id=key_data.get("key_id"), agent_id=agent_id, session_id=session_id,
            latency_ms=int((time.monotonic() - request_start_ms) * 1000),
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "PROVIDER_UNKNOWN_ERROR",
                    "message": "Provider request failed",
                    "provider": provider_name
                }
            }
        )

