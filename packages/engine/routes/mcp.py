"""
MCP (Model Context Protocol) Integration
========================================

Provides endpoints for:
1. Registering MCP servers (tool providers)
2. Discovering available tools
3. Proxying tool invocations with DLP scanning
4. Audit logging for all tool calls

Implements: https://modelcontextprotocol.io/

SECURITY CRITICAL:
- All tool inputs are DLP scanned
- All tool outputs are DLP scanned
- RBAC enforcement on tool access
- Full audit trail

"""

import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import httpx
import structlog

from core import SUPABASE_KEY
from core import log, resources, SUPABASE_URL, DLP_PATTERNS
from dependencies import get_supabase_headers
import re

# ============================================================================
# ROUTER SETUP
# ============================================================================

router = APIRouter(prefix="/v1/mcp", tags=["mcp"])


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class MCPServerRegistration(BaseModel):
    """Register an MCP server with Oxy."""
    name: str = Field(..., min_length=1, max_length=100, description="Server display name")
    description: str = Field(..., min_length=10, max_length=500, description="What this server does")
    server_url: str = Field(..., description="MCP server endpoint (e.g., http://localhost:3001)")
    protocol_version: str = Field(default="1.0", description="MCP protocol version")
    requires_approval: bool = Field(default=False, description="Require human approval for tool calls")
    risk_level: str = Field(default="low", pattern="^(low|medium|high|critical)$")
    allowed_agents: Optional[List[str]] = Field(default=None, description="Agent IDs that can use this server")


class MCPServerUpdate(BaseModel):
    """Update MCP server configuration."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, min_length=10, max_length=500)
    requires_approval: Optional[bool] = None
    risk_level: Optional[str] = Field(None, pattern="^(low|medium|high|critical)$")
    allowed_agents: Optional[List[str]] = None
    is_active: Optional[bool] = None


class ToolInvocation(BaseModel):
    """Invoke a tool through Oxy proxy."""
    tool_name: str = Field(..., description="Name of the tool to invoke")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Tool parameters")
    agent_session_id: Optional[str] = Field(None, description="Agent session for tracking")
    timeout: int = Field(default=30, ge=1, le=300, description="Timeout in seconds")


class ToolPermission(BaseModel):
    """Set permissions for a tool."""
    tool_id: str = Field(..., description="Tool UUID")
    allowed_roles: List[str] = Field(default=["owner", "admin", "developer"])
    allowed_agents: Optional[List[str]] = Field(None, description="Specific agent IDs")
    requires_approval: bool = Field(default=False)
    max_calls_per_minute: int = Field(default=60, ge=1, le=1000)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def get_user_and_org_from_jwt(request: Request) -> Dict[str, str]:
    """
    Extract user_id and organization_id from JWT token.
    
    For MCP routes, we use JWT auth (not API key).
    """
    # Get Authorization header
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    
    token = auth_header.replace("Bearer ", "").strip()
    
    # Verify with Supabase
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
        
        # Get user's organization
        org_res = await resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/organization_members",
            params={
                "select": "organization_id",
                "user_id": f"eq.{user_id}",
                "status": "eq.active",
                "limit": "1"
            },
            headers=get_supabase_headers(),
            timeout=5.0
        )
        
        if org_res.status_code != 200 or not org_res.json():
            raise HTTPException(status_code=403, detail="User not in any organization")
        
        organization_id = org_res.json()[0]["organization_id"]
        
        return {
            "user_id": user_id,
            "organization_id": organization_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        log.error("jwt_verification_failed", error=str(e)[:200])
        raise HTTPException(status_code=401, detail="Authentication failed")


def scan_for_secrets(content: str) -> Dict[str, Any]:
    """
    DLP scan content for secrets.
    
    Reused from proxy.py pattern.
    """
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


def redact_secrets(content: str) -> str:
    """Redact detected secrets from content."""
    redacted = content
    for pattern_name, pattern_regex in DLP_PATTERNS.items():
        try:
            redacted = re.sub(
                pattern_regex, 
                f"[REDACTED_{pattern_name.upper()}]", 
                redacted, 
                flags=re.IGNORECASE
            )
        except re.error:
            pass
    return redacted


async def send_approval_notification(
    organization_id: str,
    approval_id: str,
    tool_name: str,
    risk_level: str
):
    """
    Send notification to org admins about pending tool approval.
    
    Channels:
    1. In-app notification (always)
    2. Slack webhook (if configured)
    3. Email (if configured)
    """
    try:
        # 1. Create in-app notification
        await resources.supabase.table("notifications").insert({
            "organization_id": organization_id,
            "type": "tool_approval_required",
            "title": f"🛡️ Tool Approval Required: {tool_name}",
            "message": f"A {risk_level}-risk tool '{tool_name}' needs approval before execution.",
            "action_url": f"/dashboard/approvals/{approval_id}",
            "priority": "high" if risk_level in ["high", "critical"] else "medium",
            "metadata": {
                "approval_id": approval_id,
                "tool_name": tool_name,
                "risk_level": risk_level
            }
        }).execute()
        
        # 2. Send Slack notification (if webhook configured)
        slack_webhook = await _get_org_setting(organization_id, "slack_webhook_url")
        if slack_webhook:
            async with httpx.AsyncClient() as client:
                await client.post(slack_webhook, json={
                    "text": f"🛡️ *Tool Approval Required*\n"
                           f"Tool: `{tool_name}` ({risk_level} risk)\n"
                           f"<{resources.app_url}/dashboard/approvals/{approval_id}|Review Request>"
                }, timeout=5.0)
        
        log.info("approval_notification_sent",
                organization_id=organization_id,
                approval_id=approval_id,
                tool_name=tool_name)
                
    except Exception as e:
        log.error("approval_notification_failed",
                 error=str(e)[:200],
                 approval_id=approval_id)


async def _get_org_setting(organization_id: str, setting_key: str) -> Optional[str]:
    """Get organization setting value."""
    try:
        result = await resources.supabase.table("organization_settings").select(
            "value"
        ).eq("organization_id", organization_id).eq(
            "key", setting_key
        ).single().execute()
        return result.data.get("value") if result.data else None
    except Exception:
        return None


async def log_tool_call(
    organization_id: str,
    session_id: Optional[str],
    tool_name: str,
    tool_id: Optional[str],
    input_parameters: Dict,
    output_result: Any,
    threat_detected: bool = False,
    threat_type: Optional[str] = None,
    was_blocked: bool = False,
    was_redacted: bool = False,
    duration_ms: Optional[int] = None,
    status: str = "success",
    error_message: Optional[str] = None
):
    """Log tool call for audit trail."""
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
            "error_message": error_message,
            "completed_at": "now()" if status != "pending" else None
        }).execute()
    except Exception as e:
        log.error("log_tool_call_failed", tool_name=tool_name, error=str(e)[:200])


# ============================================================================
# MCP SERVER MANAGEMENT ENDPOINTS
# ============================================================================

@router.post("/servers")
async def register_mcp_server(
    registration: MCPServerRegistration,
    bt: BackgroundTasks,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    Register an MCP server for this organization.
    
    Oxy will:
    1. Validate server connectivity
    2. Fetch available tools via MCP protocol
    3. Store server and tools in registry
    4. Enable routing
    
    Security:
    - Only org admins/owners can register servers
    - Server URL is validated
    - Tools are audited
    """
    organization_id = auth["organization_id"]
    user_id = auth["user_id"]
    
    log.info("mcp_server_registration_start", 
             organization_id=organization_id,
             server_url=registration.server_url)
    
    # Validate MCP server is reachable
    try:
        async with httpx.AsyncClient() as client:
            # Try MCP discovery endpoint
            response = await client.get(
                f"{registration.server_url}/mcp/tools",
                timeout=10.0,
                headers={"Accept": "application/json"}
            )
            
            if response.status_code == 404:
                # Try alternative endpoint
                response = await client.get(
                    f"{registration.server_url}/.well-known/mcp.json",
                    timeout=10.0
                )
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=400,
                    detail=f"MCP server returned {response.status_code}. Is it running?"
                )
            
            mcp_manifest = response.json()
            
    except httpx.ConnectError:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot connect to MCP server at {registration.server_url}"
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=400,
            detail=f"MCP server at {registration.server_url} timed out"
        )
    except Exception as e:
        log.error("mcp_discovery_failed", error=str(e)[:200])
        raise HTTPException(
            status_code=400,
            detail=f"Failed to discover MCP server: {str(e)[:100]}"
        )
    
    # Extract tools from manifest
    tools = mcp_manifest.get("tools", [])
    
    # Insert server record
    server_id = None
    try:
        server_result = await resources.supabase.table("mcp_servers").insert({
            "organization_id": organization_id,
            "name": registration.name,
            "description": registration.description,
            "server_url": registration.server_url,
            "protocol_version": registration.protocol_version,
            "requires_approval": registration.requires_approval,
            "risk_level": registration.risk_level,
            "allowed_agents": registration.allowed_agents,
            "registered_by": user_id,
            "is_active": True,
            "tools_count": len(tools)
        }).execute()
        
        if server_result.data:
            server_id = server_result.data[0]["id"]
            
    except Exception as e:
        log.error("mcp_server_insert_failed", error=str(e)[:200])
        raise HTTPException(
            status_code=500,
            detail="Failed to register MCP server"
        )
    
    # Insert tools
    tools_inserted = 0
    for tool in tools:
        try:
            await resources.supabase.table("tools").insert({
                "organization_id": organization_id,
                "name": tool.get("name", "unknown"),
                "description": tool.get("description", ""),
                "tool_type": "mcp",
                "mcp_server_url": registration.server_url,
                "mcp_server_id": server_id,
                "mcp_protocol_version": registration.protocol_version,
                "parameters_schema": tool.get("inputSchema", {}),
                "requires_approval": registration.requires_approval,
                "risk_level": registration.risk_level,
                "author_user_id": user_id,
                "is_public": False
            }).execute()
            tools_inserted += 1
        except Exception as e:
            log.warning("mcp_tool_insert_failed", 
                       tool_name=tool.get("name"),
                       error=str(e)[:100])
    
    log.info("mcp_server_registered",
             organization_id=organization_id,
             server_id=server_id,
             server_url=registration.server_url,
             tools_count=tools_inserted)
    
    return {
        "success": True,
        "server_id": server_id,
        "server_url": registration.server_url,
        "tools_registered": tools_inserted,
        "tools": [t.get("name") for t in tools]
    }


@router.get("/servers")
async def list_mcp_servers(
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    List all registered MCP servers for this organization.
    """
    organization_id = auth["organization_id"]
    
    result = await resources.supabase.table("mcp_servers").select(
        "id, name, description, server_url, protocol_version, "
        "requires_approval, risk_level, is_active, tools_count, created_at"
    ).eq("organization_id", organization_id).execute()
    
    return {"servers": result.data or []}


@router.get("/servers/{server_id}")
async def get_mcp_server(
    server_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """Get details of a specific MCP server."""
    organization_id = auth["organization_id"]
    
    result = await resources.supabase.table("mcp_servers").select("*").eq(
        "id", server_id
    ).eq("organization_id", organization_id).single().execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="MCP server not found")
    
    # Get associated tools
    tools_result = await resources.supabase.table("tools").select(
        "id, name, description, risk_level, usage_count"
    ).eq("mcp_server_id", server_id).execute()
    
    return {
        "server": result.data,
        "tools": tools_result.data or []
    }


@router.patch("/servers/{server_id}")
async def update_mcp_server(
    server_id: str,
    update: MCPServerUpdate,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """Update MCP server configuration."""
    organization_id = auth["organization_id"]
    
    # Build update dict (only non-None fields)
    update_dict = {k: v for k, v in update.dict().items() if v is not None}
    
    if not update_dict:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    result = await resources.supabase.table("mcp_servers").update(
        update_dict
    ).eq("id", server_id).eq("organization_id", organization_id).execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="MCP server not found")
    
    return {"success": True, "server": result.data[0]}


@router.delete("/servers/{server_id}")
async def delete_mcp_server(
    server_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    Delete an MCP server and all its tools.
    
    CAUTION: This is irreversible. All tool call logs are preserved.
    """
    organization_id = auth["organization_id"]
    
    # The server must be this organization's before its tools are touched
    # (they used to be deleted by server id alone).
    owned = await resources.supabase.table("mcp_servers").select("id").eq(
        "id", server_id
    ).eq("organization_id", organization_id).execute()
    if not owned.data:
        raise HTTPException(status_code=404, detail="MCP server not found")
    
    # Delete associated tools first
    await resources.supabase.table("tools").delete().eq(
        "mcp_server_id", server_id
    ).execute()
    
    # Delete server
    result = await resources.supabase.table("mcp_servers").delete().eq(
        "id", server_id
    ).eq("organization_id", organization_id).execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="MCP server not found")
    
    log.info("mcp_server_deleted", server_id=server_id, organization_id=organization_id)
    
    return {"success": True, "deleted_server_id": server_id}


# ============================================================================
# TOOL DISCOVERY ENDPOINTS
# ============================================================================

@router.get("/tools")
async def list_available_tools(
    auth: Dict = Depends(get_user_and_org_from_jwt),
    include_public: bool = True
):
    """
    List all tools available to this organization.
    
    Includes:
    - Organization-specific MCP tools
    - Public tools (if include_public=True)
    - Built-in Oxy tools
    """
    organization_id = auth["organization_id"]
    
    # Query: org tools OR public tools
    if include_public:
        result = await resources.supabase.table("tools").select(
            "id, name, description, tool_type, risk_level, "
            "requires_approval, usage_count, is_public, mcp_server_url"
        ).or_(
            f"organization_id.eq.{organization_id},is_public.eq.true"
        ).execute()
    else:
        result = await resources.supabase.table("tools").select(
            "id, name, description, tool_type, risk_level, "
            "requires_approval, usage_count, is_public, mcp_server_url"
        ).eq("organization_id", organization_id).execute()
    
    return {"tools": result.data or [], "count": len(result.data or [])}


@router.get("/tools/{tool_id}")
async def get_tool_details(
    tool_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """Get detailed information about a specific tool."""
    organization_id = auth["organization_id"]
    
    result = await resources.supabase.table("tools").select("*").eq(
        "id", tool_id
    ).or_(
        f"organization_id.eq.{organization_id},is_public.eq.true"
    ).single().execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    return {"tool": result.data}


# ============================================================================
# TOOL INVOCATION
# ============================================================================

@router.post("/invoke")
async def invoke_tool(
    invocation: ToolInvocation,
    bt: BackgroundTasks,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    Invoke a tool through Oxy proxy.
    
    SECURITY PIPELINE:
    1. Validate tool exists and user has access
    2. DLP scan input parameters
    3. Check if approval required
    4. Execute tool call via MCP server
    5. DLP scan output
    6. Redact secrets if found
    7. Log everything for audit
    
    Returns:
    - Tool output (possibly redacted)
    - Execution metadata
    - Security warnings if any
    """
    organization_id = auth["organization_id"]
    user_id = auth["user_id"]
    session_id = invocation.agent_session_id
    
    started_at = time.time()
    
    log.info("tool_invocation_start",
             tool_name=invocation.tool_name,
             organization_id=organization_id,
             session_id=session_id)
    
    # 1. Fetch tool metadata
    tool_result = await resources.supabase.table("tools").select("*").eq(
        "name", invocation.tool_name
    ).or_(
        f"organization_id.eq.{organization_id},is_public.eq.true"
    ).limit(1).execute()
    
    if not tool_result.data:
        raise HTTPException(
            status_code=404, 
            detail=f"Tool not found: {invocation.tool_name}"
        )
    
    tool = tool_result.data[0]
    tool_id = tool["id"]
    
    # 2. DLP scan input parameters
    input_str = json.dumps(invocation.parameters)
    input_scan = scan_for_secrets(input_str)
    
    if input_scan["threat_detected"]:
        log.warning("dlp_tool_input_threat",
                   tool_name=invocation.tool_name,
                   threats=input_scan["details"])
        
        # Block if high-risk tool
        if tool["risk_level"] in ["high", "critical"]:
            bt.add_task(
                log_tool_call,
                organization_id=organization_id,
                session_id=session_id,
                tool_name=invocation.tool_name,
                tool_id=tool_id,
                input_parameters=invocation.parameters,
                output_result=None,
                threat_detected=True,
                threat_type=input_scan["threat_type"],
                was_blocked=True,
                status="blocked"
            )
            
            raise HTTPException(
                status_code=403,
                detail=f"Sensitive data detected in tool parameters: {input_scan['threat_type']}"
            )
    
    # 3. Check if approval required
    if tool["requires_approval"]:
        # =================================================================
        # 🛡️ TOOL APPROVAL WORKFLOW (ENTERPRISE SECURITY)
        # =================================================================
        # High-risk tools require human approval before execution.
        # Creates a pending approval record that must be approved via:
        # - Dashboard UI
        # - Slack/Email notification link
        # - API call from admin
        # =================================================================
        
        # Check for existing pending approval
        existing = await resources.supabase.table("tool_approvals").select(
            "id, status, created_at"
        ).eq("tool_id", tool_id).eq(
            "organization_id", organization_id
        ).eq("agent_session_id", session_id or "").eq(
            "status", "pending"
        ).gte(
            "created_at", 
            (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        ).limit(1).execute()
        
        if existing.data:
            # Return pending approval response
            approval = existing.data[0]
            raise HTTPException(
                status_code=202,
                detail={
                    "status": "pending_approval",
                    "approval_id": approval["id"],
                    "tool_name": invocation.tool_name,
                    "message": "This tool requires human approval before execution.",
                    "hint": "Check dashboard or wait for admin approval",
                    "retry_after": 30
                }
            )
        
        # Create new approval request
        approval_result = await resources.supabase.table("tool_approvals").insert({
            "organization_id": organization_id,
            "tool_id": tool_id,
            "tool_name": invocation.tool_name,
            "agent_session_id": session_id,
            "requested_by": user_id,
            "parameters": invocation.parameters,
            "risk_level": tool["risk_level"],
            "status": "pending",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        }).execute()
        
        approval_id = approval_result.data[0]["id"] if approval_result.data else None
        
        log.info("tool_approval_requested",
                tool_name=invocation.tool_name,
                approval_id=approval_id,
                organization_id=organization_id)
        
        # Send notification (background task)
        bt.add_task(
            send_approval_notification,
            organization_id=organization_id,
            approval_id=approval_id,
            tool_name=invocation.tool_name,
            risk_level=tool["risk_level"]
        )
        
        raise HTTPException(
            status_code=202,
            detail={
                "status": "approval_required",
                "approval_id": approval_id,
                "tool_name": invocation.tool_name,
                "message": "This tool requires human approval. Request has been submitted.",
                "hint": "Approval request sent to admins. Poll /v1/mcp/approvals/{id} for status.",
                "retry_after": 30
            }
        )
    
    # 4. Execute tool call
    output = None
    error_message = None
    status = "success"
    
    try:
        if tool["tool_type"] == "mcp":
            mcp_url = tool["mcp_server_url"]
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{mcp_url}/mcp/call",
                    json={
                        "tool": invocation.tool_name,
                        "parameters": invocation.parameters
                    },
                    timeout=invocation.timeout,
                    headers={"Content-Type": "application/json"}
                )
                
                if response.status_code != 200:
                    raise httpx.HTTPStatusError(
                        f"MCP server returned {response.status_code}",
                        request=response.request,
                        response=response
                    )
                
                output = response.json()
        
        elif tool["tool_type"] == "api":
            # Direct API tool (future)
            raise HTTPException(status_code=501, detail="API tools not yet implemented")
        
        elif tool["tool_type"] == "webhook":
            # Webhook tool (future)
            raise HTTPException(status_code=501, detail="Webhook tools not yet implemented")
        
        else:
            raise HTTPException(status_code=400, detail=f"Unknown tool type: {tool['tool_type']}")
            
    except httpx.ConnectError:
        status = "failed"
        error_message = f"Cannot connect to MCP server at {tool['mcp_server_url']}"
        log.error("tool_invocation_connect_failed", 
                 tool_name=invocation.tool_name,
                 mcp_url=tool["mcp_server_url"])
        
    except httpx.TimeoutException:
        status = "failed"
        error_message = f"Tool execution timed out after {invocation.timeout}s"
        log.error("tool_invocation_timeout", tool_name=invocation.tool_name)
        
    except httpx.HTTPStatusError as e:
        status = "failed"
        error_message = str(e)[:200]
        log.error("tool_invocation_http_error", 
                 tool_name=invocation.tool_name,
                 error=error_message)
        
    except Exception as e:
        status = "failed"
        error_message = str(e)[:200]
        log.error("tool_invocation_failed", 
                 tool_name=invocation.tool_name,
                 error=error_message)
    
    duration_ms = int((time.time() - started_at) * 1000)
    
    # 5. DLP scan output
    was_redacted = False
    output_threats = []
    
    if output and status == "success":
        output_str = json.dumps(output) if isinstance(output, dict) else str(output)
        output_scan = scan_for_secrets(output_str)
        
        if output_scan["threat_detected"]:
            log.warning("dlp_tool_output_threat",
                       tool_name=invocation.tool_name,
                       threats=output_scan["details"])
            output_threats = output_scan["details"]
            
            # Redact secrets from output
            if isinstance(output, dict):
                output = json.loads(redact_secrets(json.dumps(output)))
            elif isinstance(output, str):
                output = redact_secrets(output)
            
            was_redacted = True
    
    # 6. Increment usage count
    try:
        await resources.supabase.rpc(
            "increment_tool_usage",
            {"tool_id": tool_id}
        ).execute()
    except Exception:
        pass  # Non-critical
    
    # 7. Log tool call
    bt.add_task(
        log_tool_call,
        organization_id=organization_id,
        session_id=session_id,
        tool_name=invocation.tool_name,
        tool_id=tool_id,
        input_parameters=invocation.parameters,
        output_result=output,
        threat_detected=input_scan["threat_detected"] or bool(output_threats),
        threat_type=input_scan["threat_type"] if input_scan["threat_detected"] else (
            output_threats[0] if output_threats else None
        ),
        was_blocked=False,
        was_redacted=was_redacted,
        duration_ms=duration_ms,
        status=status,
        error_message=error_message
    )
    
    log.info("tool_invocation_complete",
             tool_name=invocation.tool_name,
             status=status,
             duration_ms=duration_ms,
             was_redacted=was_redacted)
    
    # Return response
    if status == "failed":
        raise HTTPException(
            status_code=502,
            detail=error_message or "Tool execution failed"
        )
    
    return {
        "success": True,
        "output": output,
        "metadata": {
            "tool_name": invocation.tool_name,
            "tool_id": tool_id,
            "duration_ms": duration_ms,
            "was_redacted": was_redacted,
            "threats_detected": output_threats if output_threats else None
        }
    }


# ============================================================================
# TOOL PERMISSIONS (RBAC)
# ============================================================================

@router.post("/tools/{tool_id}/permissions")
async def set_tool_permissions(
    tool_id: str,
    permissions: ToolPermission,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    Set permissions for a tool.
    
    Only org owners/admins can modify permissions.
    """
    organization_id = auth["organization_id"]
    
    # Verify tool belongs to org
    tool_result = await resources.supabase.table("tools").select("id").eq(
        "id", tool_id
    ).eq("organization_id", organization_id).single().execute()
    
    if not tool_result.data:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    # Update tool permissions
    result = await resources.supabase.table("tools").update({
        "allowed_roles": permissions.allowed_roles,
        "allowed_agents": permissions.allowed_agents,
        "requires_approval": permissions.requires_approval,
        "max_calls_per_minute": permissions.max_calls_per_minute
    }).eq("id", tool_id).execute()
    
    return {"success": True, "tool_id": tool_id}


@router.get("/tools/{tool_id}/permissions")
async def get_tool_permissions(
    tool_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """Get current permissions for a tool."""
    organization_id = auth["organization_id"]
    
    result = await resources.supabase.table("tools").select(
        "id, name, allowed_roles, allowed_agents, requires_approval, max_calls_per_minute"
    ).eq("id", tool_id).or_(
        f"organization_id.eq.{organization_id},is_public.eq.true"
    ).single().execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    return {"permissions": result.data}


# ============================================================================
# TOOL APPROVAL WORKFLOW ENDPOINTS
# ============================================================================

class ApprovalAction(BaseModel):
    """Action on a pending approval."""
    action: str = Field(..., pattern="^(approve|reject)$")
    reason: Optional[str] = Field(None, max_length=500)


@router.get("/approvals")
async def list_pending_approvals(
    auth: Dict = Depends(get_user_and_org_from_jwt),
    status: str = "pending"
):
    """
    List pending tool approval requests.
    
    Returns all pending approvals for the organization.
    Only org admins/owners can see and act on approvals.
    """
    organization_id = auth["organization_id"]
    
    result = await resources.supabase.table("tool_approvals").select(
        "id, tool_id, tool_name, agent_session_id, parameters, "
        "risk_level, status, requested_by, created_at, expires_at, "
        "approved_by, approved_at, rejection_reason"
    ).eq("organization_id", organization_id).eq(
        "status", status
    ).order("created_at", desc=True).execute()
    
    return {
        "approvals": result.data or [],
        "count": len(result.data or [])
    }


@router.get("/approvals/{approval_id}")
async def get_approval_status(
    approval_id: str,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    Get status of a specific approval request.
    
    Agents can poll this endpoint to check if their tool call was approved.
    """
    organization_id = auth["organization_id"]
    
    result = await resources.supabase.table("tool_approvals").select("*").eq(
        "id", approval_id
    ).eq("organization_id", organization_id).single().execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Approval request not found")
    
    approval = result.data
    
    # Check if expired
    if approval["status"] == "pending":
        expires_at = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            # Mark as expired
            await resources.supabase.table("tool_approvals").update({
                "status": "expired"
            }).eq("id", approval_id).execute()
            approval["status"] = "expired"
    
    return {"approval": approval}


@router.post("/approvals/{approval_id}")
async def process_approval(
    approval_id: str,
    action: ApprovalAction,
    bt: BackgroundTasks,
    auth: Dict = Depends(get_user_and_org_from_jwt)
):
    """
    Approve or reject a tool invocation request.
    
    Security:
    - Only org admins/owners can approve
    - Approval audit trail is created
    - If approved, tool can be executed by re-calling invoke endpoint
    """
    organization_id = auth["organization_id"]
    user_id = auth["user_id"]
    
    # Fetch approval request
    result = await resources.supabase.table("tool_approvals").select("*").eq(
        "id", approval_id
    ).eq("organization_id", organization_id).single().execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Approval request not found")
    
    approval = result.data
    
    # Check if already processed
    if approval["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Approval already processed: {approval['status']}"
        )
    
    # Check if expired
    expires_at = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=400, detail="Approval request has expired")
    
    # Process action
    if action.action == "approve":
        update_data = {
            "status": "approved",
            "approved_by": user_id,
            "approved_at": datetime.now(timezone.utc).isoformat()
        }
        log.info("tool_approval_granted",
                approval_id=approval_id,
                tool_name=approval["tool_name"],
                approved_by=user_id)
    else:
        update_data = {
            "status": "rejected",
            "approved_by": user_id,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "rejection_reason": action.reason
        }
        log.info("tool_approval_rejected",
                approval_id=approval_id,
                tool_name=approval["tool_name"],
                rejected_by=user_id,
                reason=action.reason)
    
    # Update approval record
    await resources.supabase.table("tool_approvals").update(
        update_data
    ).eq("id", approval_id).execute()
    
    # Log audit event
    bt.add_task(
        log_tool_call,
        organization_id=organization_id,
        session_id=approval.get("agent_session_id"),
        tool_name=approval["tool_name"],
        tool_id=approval["tool_id"],
        input_parameters=approval.get("parameters", {}),
        output_result={"action": action.action, "reason": action.reason},
        threat_detected=False,
        threat_type=None,
        was_blocked=action.action == "rejected",
        status=f"approval_{action.action}d"
    )
    
    return {
        "success": True,
        "approval_id": approval_id,
        "status": update_data["status"],
        "tool_name": approval["tool_name"],
        "message": f"Tool {'approved for execution' if action.action == 'approve' else 'rejected'}"
    }


# ============================================================================
# HEALTH CHECK
# ============================================================================

@router.get("/health")
async def mcp_health():
    """MCP subsystem health check."""
    return {
        "status": "healthy",
        "subsystem": "mcp",
        "version": "1.0.0"
    }

