"""
Agent Management API
=====================

CRUD operations for AI agents + real-time control.

Features:
- Create/configure agents
- Start/stop agent sessions
- Real-time status monitoring
- Kill switch (emergency stop)
- Per-agent budget management
- Session history and analytics

"""

import json
import time
import hashlib
from typing import Dict, Any, List, Optional
from decimal import Decimal
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import structlog

from core import SUPABASE_KEY
from core import log, resources, SUPABASE_URL
from core.control import cache_agent_status, cache_org_status, mark_session_terminated
from dependencies import get_supabase_headers

# ============================================================================
# ROUTER SETUP
# ============================================================================

router = APIRouter(prefix="/v1/agents", tags=["agents"])


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class AgentCreate(BaseModel):
    """Create a new agent."""
    name: str = Field(..., min_length=1, max_length=100, description="Agent display name")
    description: Optional[str] = Field(None, max_length=500)
    project_id: str = Field(..., description="Project UUID")
    agent_type: str = Field(default="custom", pattern="^(custom|code_review|support|devops|security|data_analysis|content|research|automation)$")
    
    # Configuration
    default_model: str = Field(default="gpt-4o")
    allowed_models: List[str] = Field(default=["gpt-4o", "claude-3-5-sonnet", "gemini-1.5-pro"])
    allowed_tools: List[str] = Field(default=[])
    max_tokens_per_request: int = Field(default=4096, ge=1, le=128000)
    max_requests_per_session: int = Field(default=100, ge=1, le=10000)
    max_tool_calls_per_session: int = Field(default=50, ge=1, le=1000)
    
    # Budget
    budget_limit: float = Field(default=0.00, ge=0, description="Budget limit in USD. 0 = unlimited")
    reset_period: str = Field(default="monthly", pattern="^(daily|weekly|monthly|never)$")
    
    # Metadata
    tags: List[str] = Field(default=[])
    settings: Dict[str, Any] = Field(default={})


class AgentUpdate(BaseModel):
    """Update agent configuration."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    agent_type: Optional[str] = Field(None, pattern="^(custom|code_review|support|devops|security|data_analysis|content|research|automation)$")
    
    # Configuration
    default_model: Optional[str] = None
    allowed_models: Optional[List[str]] = None
    allowed_tools: Optional[List[str]] = None
    max_tokens_per_request: Optional[int] = Field(None, ge=1, le=128000)
    max_requests_per_session: Optional[int] = Field(None, ge=1, le=10000)
    max_tool_calls_per_session: Optional[int] = Field(None, ge=1, le=1000)
    
    # Budget
    budget_limit: Optional[float] = Field(None, ge=0)
    reset_period: Optional[str] = Field(None, pattern="^(daily|weekly|monthly|never)$")
    
    # Status
    status: Optional[str] = Field(None, pattern="^(active|suspended|disabled)$")
    
    # Metadata
    tags: Optional[List[str]] = None
    settings: Optional[Dict[str, Any]] = None


class SessionCreate(BaseModel):
    """Start a new agent session."""
    session_type: str = Field(default="task", pattern="^(task|conversation|batch|scheduled|triggered)$")
    name: Optional[str] = Field(None, max_length=100)
    initial_context: Dict[str, Any] = Field(default={})
    metadata: Dict[str, Any] = Field(default={})


class SessionKill(BaseModel):
    """Kill an agent session."""
    reason: str = Field(default="Manual termination", max_length=500)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def get_user_and_org_from_jwt(request: Request) -> Dict[str, str]:
    """Extract user_id and organization_id from JWT token."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    
    token = auth_header.replace("Bearer ", "").strip()
    
    try:
        res = await resources.http_client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": SUPABASE_KEY
            },
            timeout=5.0
        )
        
        if res.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        
        user_data = res.json()
        user_id = user_data.get("id")
        
        if not user_id:
            raise HTTPException(status_code=401, detail="User ID not found in token")
        
        org_res = await resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/organization_members",
            params={
                "select": "organization_id,role",
                "user_id": f"eq.{user_id}",
                "status": "eq.active",
                "limit": "1"
            },
            headers=get_supabase_headers(),
            timeout=5.0
        )
        
        if org_res.status_code != 200 or not org_res.json():
            raise HTTPException(status_code=403, detail="User not in any organization")
        
        org_data = org_res.json()[0]
        
        return {
            "user_id": user_id,
            "organization_id": org_data["organization_id"],
            "role": org_data["role"]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        log.error("jwt_verification_failed", error=str(e)[:200])
        raise HTTPException(status_code=401, detail="Authentication failed")


async def publish_kill_signal(session_id: str):
    """
    Publish kill signal to Redis for the proxy to check.
    
    The proxy checks this key before processing requests.
    """
    try:
        await mark_session_terminated(session_id)
        log.info("kill_signal_published", session_id=session_id)
    except Exception as e:
        log.error("kill_signal_publish_failed", session_id=session_id, error=str(e)[:200])


# ============================================================================
# AGENT CRUD ENDPOINTS
# ============================================================================

@router.post("")
async def create_agent(
    agent: AgentCreate,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    Create a new AI agent.
    
    Only org admins/owners can create agents.
    Agent limits enforced by subscription tier.
    """
    organization_id = auth["organization_id"]
    user_id = auth["user_id"]
    role = auth["role"]
    
    if role not in ["owner", "admin"]:
        raise HTTPException(status_code=403, detail="Only admins can create agents")
    
    # ========================================================================
    # Agent limits per plan; keep in line with PLAN_LIMITS in routes/billing.py.
    # ========================================================================
    AGENT_LIMITS = {
        'free': 3,
        'starter': 10,
        'growth': 50,
        'business': 9999,
        'enterprise': 9999,
    }

    # The organization's plan lives on organizations.subscription_plan, as the
    # gateway reads it (subscriptions are per user and have no organization_id).
    org_res = await resources.supabase.table("organizations").select(
        "subscription_plan"
    ).eq("id", organization_id).limit(1).execute()

    tier = (org_res.data[0].get("subscription_plan") if org_res.data else None) or "free"
    max_agents = AGENT_LIMITS.get(tier, 3)
    
    # Count existing agents
    count_res = await resources.supabase.table("agents").select(
        "id"
    ).eq("organization_id", organization_id).neq("status", "disabled").execute()

    existing_count = len(count_res.data or [])
    
    if existing_count >= max_agents:
        tier_names = {
            'free': 'Free',
            'starter': 'Starter',
            'growth': 'Growth',
            'business': 'Business',
            'enterprise': 'Enterprise'
        }
        log.warning("agent_limit_reached",
                   organization_id=organization_id,
                   tier=tier,
                   existing_count=existing_count,
                   max_agents=max_agents)
        raise HTTPException(
            status_code=403,
            detail=f"Agent limit reached. Your {tier_names.get(tier, 'Free')} plan allows {max_agents} agent(s). You currently have {existing_count}. An operator can raise the plan tier to allow more agents."
        )
    
    # Verify project belongs to organization
    project_res = await resources.supabase.table("projects").select("id").eq(
        "id", agent.project_id
    ).eq("organization_id", organization_id).single().execute()
    
    if not project_res.data:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Create agent
    try:
        result = await resources.supabase.table("agents").insert({
            "organization_id": organization_id,
            "project_id": agent.project_id,
            "name": agent.name,
            "description": agent.description,
            "agent_type": agent.agent_type,
            "default_model": agent.default_model,
            "allowed_models": agent.allowed_models,
            "allowed_tools": agent.allowed_tools,
            "max_tokens_per_request": agent.max_tokens_per_request,
            "max_requests_per_session": agent.max_requests_per_session,
            "max_tool_calls_per_session": agent.max_tool_calls_per_session,
            "budget_limit": agent.budget_limit,
            "reset_period": agent.reset_period,
            "tags": agent.tags,
            "settings": agent.settings,
            "created_by": user_id,
            "status": "active"
        }).execute()
        
        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to create agent")
        
        log.info("agent_created",
                organization_id=organization_id,
                agent_id=result.data[0]["id"],
                agent_name=agent.name)
        
        return {"success": True, "agent": result.data[0]}
        
    except Exception as e:
        if "unique constraint" in str(e).lower():
            raise HTTPException(status_code=409, detail="Agent with this name already exists")
        log.error("agent_create_failed", error=str(e)[:200])
        raise HTTPException(status_code=500, detail="Failed to create agent")


@router.get("")
async def list_agents(
    auth: Dict = Depends(get_user_and_org_from_jwt),
    status: Optional[str] = None,
    agent_type: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """List all agents for this organization."""
    organization_id = auth["organization_id"]
    
    query = resources.supabase.table("agents").select(
        "id, name, description, agent_type, default_model, status, "
        "budget_limit, current_spend, total_requests, total_tool_calls, "
        "last_active_at, created_at"
    ).eq("organization_id", organization_id)
    
    if status:
        query = query.eq("status", status)
    if agent_type:
        query = query.eq("agent_type", agent_type)
    if project_id:
        query = query.eq("project_id", project_id)
    
    result = await query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    
    return {
        "agents": result.data or [],
        "count": len(result.data or []),
        "offset": offset,
        "limit": limit
    }


@router.get("/{agent_id}")
async def get_agent(
    agent_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """Get detailed information about a specific agent."""
    organization_id = auth["organization_id"]
    
    result = await resources.supabase.table("agents").select("*").eq(
        "id", agent_id
    ).eq("organization_id", organization_id).single().execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    # Get recent sessions
    sessions_result = await resources.supabase.table("agent_sessions").select(
        "id, session_type, status, started_at, ended_at, total_requests, total_cost"
    ).eq("agent_id", agent_id).order("started_at", desc=True).limit(10).execute()
    
    return {
        "agent": result.data,
        "recent_sessions": sessions_result.data or []
    }


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str,
    update: AgentUpdate,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """Update agent configuration."""
    organization_id = auth["organization_id"]
    role = auth["role"]
    
    if role not in ["owner", "admin"]:
        raise HTTPException(status_code=403, detail="Only admins can update agents")
    
    # Build update dict (only non-None fields)
    update_dict = {k: v for k, v in update.dict().items() if v is not None}
    
    if not update_dict:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    update_dict["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    result = await resources.supabase.table("agents").update(
        update_dict
    ).eq("id", agent_id).eq("organization_id", organization_id).execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    log.info("agent_updated", agent_id=agent_id, fields=list(update_dict.keys()))
    
    return {"success": True, "agent": result.data[0]}


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    Delete an agent.
    
    Only org owners can delete agents.
    Active sessions will be killed first.
    """
    organization_id = auth["organization_id"]
    role = auth["role"]
    
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only owners can delete agents")
    
    # The agent must be this organization's before anything of it is touched
    # (the sessions used to be killed by agent id alone).
    owned = await resources.supabase.table("agents").select("id").eq(
        "id", agent_id
    ).eq("organization_id", organization_id).execute()
    if not owned.data:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Kill all running sessions first: the row records who, when and why, and the
    # flag in Redis, which the gateway reads first, refuses them at once.
    now = datetime.now(timezone.utc).isoformat()
    killed = await resources.supabase.table("agent_sessions").update({
        "status": "killed",
        "kill_requested_at": now,
        "kill_requested_by": auth["user_id"],
        "kill_reason": "Agent deleted",
        "ended_at": now
    }).eq("agent_id", agent_id).eq("organization_id", organization_id).in_(
        "status", ["pending", "running"]
    ).execute()
    for session in killed.data or []:
        await mark_session_terminated(session["id"])
    
    # Delete agent (cascades to sessions, api_keys)
    result = await resources.supabase.table("agents").delete().eq(
        "id", agent_id
    ).eq("organization_id", organization_id).execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    log.info("agent_deleted", agent_id=agent_id, organization_id=organization_id)
    
    return {"success": True, "deleted_agent_id": agent_id}


# ============================================================================
# AGENT STATUS CONTROL
# ============================================================================

@router.post("/{agent_id}/pause")
async def pause_agent(
    agent_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    Pause an agent.
    
    Paused agents cannot start new sessions or process requests.
    Running sessions continue until complete.
    """
    organization_id = auth["organization_id"]
    
    result = await resources.supabase.table("agents").update({
        "status": "suspended",
        "suspend_reason": "manual",
        "suspended_until": None,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", agent_id).eq("organization_id", organization_id).eq("status", "active").execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Agent not found or already suspended")
    
    await cache_agent_status(agent_id, {"status": "suspended", "suspend_reason": "manual", "suspended_until": None})
    log.info("agent_paused", agent_id=agent_id)
    
    return {"success": True, "status": "suspended"}


@router.post("/{agent_id}/resume")
async def resume_agent(
    agent_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """Resume a suspended agent."""
    organization_id = auth["organization_id"]
    
    result = await resources.supabase.table("agents").update({
        "status": "active",
        "suspend_reason": None,
        "suspended_until": None,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", agent_id).eq("organization_id", organization_id).eq("status", "suspended").execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Agent not found or not suspended")
    
    await cache_agent_status(agent_id, {"status": "active", "suspend_reason": None, "suspended_until": None})
    log.info("agent_resumed", agent_id=agent_id)
    
    return {"success": True, "status": "active"}


# ============================================================================
# SESSION MANAGEMENT
# ============================================================================

@router.post("/{agent_id}/sessions")
async def start_session(
    agent_id: str,
    session: SessionCreate,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    Start a new agent session.
    
    Returns session_id to include in subsequent requests.
    """
    organization_id = auth["organization_id"]
    
    # Verify agent exists and is active
    agent_result = await resources.supabase.table("agents").select(
        "id, name, status, project_id, max_requests_per_session"
    ).eq("id", agent_id).eq("organization_id", organization_id).single().execute()
    
    if not agent_result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    agent = agent_result.data
    
    if agent["status"] != "active":
        raise HTTPException(status_code=409, detail=f"Agent is {agent['status']}, cannot start session")
    
    # Check concurrent sessions limit (optional)
    running_count_res = await resources.supabase.table("agent_sessions").select(
        "id", count="exact"
    ).eq("agent_id", agent_id).in_("status", ["pending", "running"]).execute()
    
    # Limit to 10 concurrent sessions per agent
    if len(running_count_res.data or []) >= 10:
        raise HTTPException(status_code=429, detail="Too many concurrent sessions for this agent")
    
    # Create session
    result = await resources.supabase.table("agent_sessions").insert({
        "agent_id": agent_id,
        "organization_id": organization_id,
        "project_id": agent["project_id"],
        "session_type": session.session_type,
        "name": session.name,
        "status": "running",
        "initial_context": session.initial_context,
        "metadata": session.metadata
    }).execute()
    
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create session")
    
    session_data = result.data[0]
    
    log.info("agent_session_started",
             agent_id=agent_id,
             session_id=session_data["id"],
             session_type=session.session_type)
    
    return {
        "success": True,
        "session": session_data,
        "headers": {
            "X-Oxy-Agent-Id": agent_id,
            "X-Oxy-Session-Id": session_data["id"],
            "X-Oxy-Agent-Name": agent["name"]
        }
    }


@router.get("/{agent_id}/sessions")
async def list_sessions(
    agent_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt),
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """List sessions for an agent."""
    organization_id = auth["organization_id"]
    
    query = resources.supabase.table("agent_sessions").select(
        "id, session_type, name, status, started_at, ended_at, "
        "total_requests, total_tokens, total_cost, total_tool_calls, "
        "error_message"
    ).eq("agent_id", agent_id).eq("organization_id", organization_id)
    
    if status:
        query = query.eq("status", status)
    
    result = await query.order("started_at", desc=True).range(offset, offset + limit - 1).execute()
    
    return {
        "sessions": result.data or [],
        "count": len(result.data or []),
        "offset": offset,
        "limit": limit
    }


@router.get("/{agent_id}/sessions/{session_id}")
async def get_session(
    agent_id: str,
    session_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """Get detailed information about a session."""
    organization_id = auth["organization_id"]
    
    result = await resources.supabase.table("agent_sessions").select("*").eq(
        "id", session_id
    ).eq("agent_id", agent_id).eq("organization_id", organization_id).single().execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Get tool calls for this session
    tool_calls = await resources.supabase.table("tool_call_logs").select(
        "id, tool_name, status, duration_ms, threat_detected, started_at"
    ).eq("agent_session_id", session_id).order("started_at", desc=True).limit(100).execute()
    
    return {
        "session": result.data,
        "tool_calls": tool_calls.data or []
    }


@router.post("/{agent_id}/sessions/{session_id}/complete")
async def complete_session(
    agent_id: str,
    session_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt),
    final_output: Optional[Dict[str, Any]] = None
):
    """Mark a session as completed."""
    organization_id = auth["organization_id"]
    
    update_data = {
        "status": "completed",
        "ended_at": datetime.now(timezone.utc).isoformat()
    }
    
    if final_output:
        update_data["final_output"] = final_output
    
    result = await resources.supabase.table("agent_sessions").update(
        update_data
    ).eq("id", session_id).eq("agent_id", agent_id).eq(
        "organization_id", organization_id
    ).in_("status", ["pending", "running"]).execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Session not found or already ended")
    
    log.info("agent_session_completed", session_id=session_id)
    
    return {"success": True, "session": result.data[0]}


# ============================================================================
# KILL SWITCH
# ============================================================================

@router.delete("/{agent_id}/sessions/{session_id}")
async def kill_session(
    agent_id: str,
    session_id: str,
    kill: SessionKill,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    🚨 KILL SWITCH: Immediately terminate an agent session.
    
    DESIGN GOAL: fast end-to-end latency (single-digit ms in internal benchmarks; no formal SLA)
    
    This:
    1. Publishes kill signal to Redis (immediate, <1ms)
    2. Marks session as 'killed' in database
    3. All in-flight requests for this session will be rejected
    4. Agent is NOT suspended (other sessions continue)
    
    Use this when:
    - Agent is stuck in a loop
    - Agent is consuming too many resources
    - Agent is behaving unexpectedly
    - Emergency stop needed
    """
    import time
    start_time = time.perf_counter()
    
    organization_id = auth["organization_id"]
    user_id = auth["user_id"]
    
    # The database row first, scoped to this organization and agent: a session id
    # from another organization matches nothing and gets no kill flag. (The flag
    # used to be set before this check, so anyone signed in could stop another
    # organization's session for an hour by its id.)
    result = await resources.supabase.table("agent_sessions").update({
        "status": "killed",
        "kill_requested_at": datetime.now(timezone.utc).isoformat(),
        "kill_requested_by": user_id,
        "kill_reason": kill.reason,
        "ended_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", session_id).eq("agent_id", agent_id).eq(
        "organization_id", organization_id
    ).in_("status", ["pending", "running"]).execute()
    
    if not result.data:
        raise HTTPException(
            status_code=404, 
            detail="Session not found or already ended"
        )
    
    # Then the Redis flag, which the gateway reads first on every request.
    flag_start = time.perf_counter()
    try:
        await mark_session_terminated(session_id)
    except Exception as e:
        log.error("kill_signal_redis_failed", session_id=session_id, error=str(e)[:100])
    redis_latency_ms = (time.perf_counter() - flag_start) * 1000
    total_latency_ms = (time.perf_counter() - start_time) * 1000
    
    log.warning("agent_session_killed",
               agent_id=agent_id,
               session_id=session_id,
               killed_by=user_id,
               reason=kill.reason,
               redis_latency_ms=round(redis_latency_ms, 2),
               total_latency_ms=round(total_latency_ms, 2))
    
    return {
        "success": True,
        "killed": True,
        "session_id": session_id,
        "reason": kill.reason,
        "message": "Session terminated. In-flight requests will be rejected.",
        "latency": {
            "kill_signal_ms": round(redis_latency_ms, 2),
            "total_ms": round(total_latency_ms, 2),
            "note": "Fast kill signal path (single-digit ms observed internally; no formal SLA)"
        }
    }


@router.post("/{agent_id}/kill-all")
async def kill_all_sessions(
    agent_id: str,
    kill: SessionKill,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    🚨 EMERGENCY: Kill ALL running sessions for an agent.
    
    DESIGN GOAL: fast parallel kill signals (single-digit ms observed internally; no formal SLA)
    """
    import time
    start_time = time.perf_counter()
    
    organization_id = auth["organization_id"]
    user_id = auth["user_id"]
    
    # Get all running sessions
    sessions_result = await resources.supabase.table("agent_sessions").select("id").eq(
        "agent_id", agent_id
    ).eq("organization_id", organization_id).in_("status", ["pending", "running"]).execute()
    
    if not sessions_result.data:
        return {"success": True, "killed_count": 0, "message": "No running sessions", "latency_ms": 0}
    
    session_ids = [s["id"] for s in sessions_result.data]
    
    # CRITICAL: Publish all kill signals to Redis FIRST (parallel)
    # Fast-path design goal: batch Redis SET operations stay in single-digit ms (no SLA)
    import asyncio
    kill_tasks = []
    for session_id in session_ids:
        kill_tasks.append(mark_session_terminated(session_id))
    
    try:
        await asyncio.gather(*kill_tasks)
    except Exception as e:
        log.error("kill_all_redis_failed", agent_id=agent_id, error=str(e)[:100])
    
    redis_latency_ms = (time.perf_counter() - start_time) * 1000
    
    # Update all sessions in database (can be slower, kill signals already sent)
    await resources.supabase.table("agent_sessions").update({
        "status": "killed",
        "kill_requested_at": datetime.now(timezone.utc).isoformat(),
        "kill_requested_by": user_id,
        "kill_reason": kill.reason,
        "ended_at": datetime.now(timezone.utc).isoformat()
    }).in_("id", session_ids).execute()
    
    total_latency_ms = (time.perf_counter() - start_time) * 1000
    
    log.warning("agent_all_sessions_killed",
               agent_id=agent_id,
               killed_count=len(session_ids),
               killed_by=user_id,
               reason=kill.reason,
               redis_latency_ms=round(redis_latency_ms, 2),
               total_latency_ms=round(total_latency_ms, 2))
    
    return {
        "success": True,
        "killed_count": len(session_ids),
        "session_ids": session_ids,
        "reason": kill.reason,
        "latency_ms": round(redis_latency_ms, 2),
        "latency": {
            "kill_signals_ms": round(redis_latency_ms, 2),
            "total_ms": round(total_latency_ms, 2),
            "note": "Fast parallel kill signals (single-digit ms observed internally; no formal SLA)"
        }
    }


# ============================================================================
# AGENT ANALYTICS
# ============================================================================

@router.get("/{agent_id}/stats")
async def get_agent_stats(
    agent_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt),
    period: str = "7d"  # 1d, 7d, 30d, 90d
):
    """Get usage statistics for an agent."""
    organization_id = auth["organization_id"]
    
    # Verify agent exists
    agent_result = await resources.supabase.table("agents").select(
        "id, name, total_requests, total_tokens, total_tool_calls, current_spend, budget_limit"
    ).eq("id", agent_id).eq("organization_id", organization_id).single().execute()
    
    if not agent_result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    # Get session stats
    sessions_result = await resources.supabase.table("agent_sessions").select(
        "status", count="exact"
    ).eq("agent_id", agent_id).execute()
    
    # Calculate session breakdown
    session_stats = {}
    for session in sessions_result.data or []:
        status = session.get("status", "unknown")
        session_stats[status] = session_stats.get(status, 0) + 1
    
    return {
        "agent": agent_result.data,
        "sessions": {
            "total": len(sessions_result.data or []),
            "by_status": session_stats
        },
        "period": period
    }


# ============================================================================
# BUDGET MANAGEMENT
# ============================================================================

@router.get("/{agent_id}/budget")
async def get_agent_budget(
    agent_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """Get budget status for an agent."""
    organization_id = auth["organization_id"]
    
    result = await resources.supabase.table("agents").select(
        "id, name, budget_limit, current_spend, reset_period, last_reset_at"
    ).eq("id", agent_id).eq("organization_id", organization_id).single().execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    agent = result.data
    
    return {
        "agent_id": agent["id"],
        "agent_name": agent["name"],
        "budget_limit": float(agent["budget_limit"]),
        "current_spend": float(agent["current_spend"]),
        "remaining": float(agent["budget_limit"]) - float(agent["current_spend"]) if agent["budget_limit"] > 0 else None,
        "usage_percent": (float(agent["current_spend"]) / float(agent["budget_limit"]) * 100) if agent["budget_limit"] > 0 else 0,
        "reset_period": agent["reset_period"],
        "last_reset_at": agent["last_reset_at"],
        "is_unlimited": agent["budget_limit"] == 0
    }


@router.post("/{agent_id}/budget/reset")
async def reset_agent_budget(
    agent_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """Manually reset an agent's budget."""
    organization_id = auth["organization_id"]
    role = auth["role"]
    
    if role not in ["owner", "admin"]:
        raise HTTPException(status_code=403, detail="Only admins can reset budgets")
    
    result = await resources.supabase.table("agents").update({
        "current_spend": 0,
        "last_reset_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", agent_id).eq("organization_id", organization_id).execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    log.info("agent_budget_reset", agent_id=agent_id, reset_by=auth["user_id"])
    
    return {"success": True, "current_spend": 0}


# ============================================================================
# HEALTH CHECK
# ============================================================================

@router.get("/health")
async def agents_health():
    """Agents subsystem health check."""
    return {
        "status": "healthy",
        "subsystem": "agents",
        "version": "1.0.0"
    }

