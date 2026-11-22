"""
COST OPTIMIZER
==============
LLM-generated cost optimization recommendations (uses Claude).

Analyzes:
- Model usage patterns
- Request inefficiencies
- Token waste (too-long prompts)
- Cache miss opportunities

Example recommendations (savings are estimates computed from your own usage):
- "Switch 20% of GPT-4 requests to GPT-4 Turbo"
- "Enable caching for repeated support prompts"
- "Use Claude-3 Haiku for code completion"

Author: Oxy contributors
License: Apache 2.0
"""

import os
import json
from datetime import datetime, timedelta, timezone
from typing import List, Dict
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
import anthropic

from core import log, resources
from dependencies import get_current_user_id, get_supabase_headers

SUPABASE_URL = os.getenv("SUPABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

router = APIRouter(prefix="/cost-optimizer", tags=["cost-optimizer"])


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class CostRecommendation(BaseModel):
    """AI-generated cost optimization recommendation."""
    action: str = Field(description="Actionable recommendation")
    reasoning: str = Field(description="Why this will save money")
    estimated_savings_usd: int = Field(description="Estimated monthly savings in USD")
    confidence: float = Field(description="Confidence score 0-1")
    priority: int = Field(description="Priority 1-3 (1=highest)")
    implementation_steps: List[str] = Field(description="How to implement this")


class UsageAnalysis(BaseModel):
    """Usage patterns analyzed for cost optimization."""
    total_requests: int
    total_cost_usd: float
    model_breakdown: Dict[str, Dict[str, float]]  # model -> {requests, cost, avg_tokens}
    period_days: int
    organization_id: str


# ============================================================================
# COST OPTIMIZATION ENGINE
# ============================================================================

async def analyze_usage_patterns(organization_id: str, days: int = 30) -> UsageAnalysis:
    """
    Fetch and aggregate usage data for an organization.
    """
    # Get all projects for organization
    projects_res = await resources.http_client.get(
        f"{SUPABASE_URL}/rest/v1/projects",
        params={
            "select": "id",
            "organization_id": f"eq.{organization_id}"
        },
        headers=get_supabase_headers()
    )
    projects_res.raise_for_status()
    projects = projects_res.json()
    
    if not projects:
        return UsageAnalysis(
            total_requests=0,
            total_cost_usd=0.0,
            model_breakdown={},
            period_days=days,
            organization_id=organization_id
        )
    
    project_ids = [p["id"] for p in projects]
    project_ids_str = ",".join(project_ids)
    
    # Get request logs (last N days)
    since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    
    logs_res = await resources.http_client.get(
        f"{SUPABASE_URL}/rest/v1/request_logs",
        params={
            "select": "model,cost_usd,prompt_tokens,completion_tokens",
            "project_id": f"in.({project_ids_str})",
            "created_at": f"gte.{since_date}"
        },
        headers=get_supabase_headers()
    )
    logs_res.raise_for_status()
    logs = logs_res.json()
    
    # Aggregate by model
    model_breakdown = {}
    total_cost = 0.0
    
    for log_entry in logs:
        model = log_entry.get("model", "unknown")
        cost = float(log_entry.get("cost_usd", 0))
        prompt_tokens = int(log_entry.get("prompt_tokens", 0))
        completion_tokens = int(log_entry.get("completion_tokens", 0))
        
        if model not in model_breakdown:
            model_breakdown[model] = {
                "requests": 0,
                "cost": 0.0,
                "total_tokens": 0
            }
        
        model_breakdown[model]["requests"] += 1
        model_breakdown[model]["cost"] += cost
        model_breakdown[model]["total_tokens"] += (prompt_tokens + completion_tokens)
        
        total_cost += cost
    
    return UsageAnalysis(
        total_requests=len(logs),
        total_cost_usd=round(total_cost, 2),
        model_breakdown=model_breakdown,
        period_days=days,
        organization_id=organization_id
    )


async def generate_recommendations_with_claude(usage: UsageAnalysis) -> List[CostRecommendation]:
    """
    Use Claude to analyze usage patterns and generate recommendations.
    """
    if not ANTHROPIC_API_KEY:
        log.warn("anthropic_api_key_missing", message="Cannot generate AI recommendations")
        return []
    
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    prompt = f"""You are an AI cost optimization expert for an LLM gateway company.

Analyze this customer's usage data:

**Period:** Last {usage.period_days} days
**Total Requests:** {usage.total_requests:,}
**Total Cost:** ${usage.total_cost_usd:.2f}

**Model Breakdown:**
{json.dumps(usage.model_breakdown, indent=2)}

**Your Task:**
Provide 3 specific, actionable recommendations to reduce costs while maintaining quality.

**Guidelines:**
1. Look for expensive models that could be replaced with cheaper alternatives
2. Identify high-volume use cases that would benefit from caching
3. Suggest token optimization strategies (shorter prompts, lower max_tokens)
4. Consider model-switching opportunities (GPT-4 → GPT-4 Turbo for simple tasks)
5. Estimate realistic monthly savings in USD

**Output Format (JSON):**
[
  {{
    "action": "Switch 30% of GPT-4 requests to GPT-4 Turbo for summarization tasks",
    "reasoning": "GPT-4 Turbo is 66% cheaper ($0.01 vs $0.03) and performs equally well for summarization",
    "estimated_savings_usd": 400,
    "confidence": 0.85,
    "priority": 1,
    "implementation_steps": [
      "Identify summarization requests (prompt analysis)",
      "Update model parameter from 'gpt-4' to 'gpt-4-turbo'",
      "Monitor quality for 7 days",
      "Scale to 100% if quality maintained"
    ]
  }},
  ...
]

Return ONLY the JSON array. No markdown, no explanations.
"""
    
    try:
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )
        
        # Parse Claude's response
        response_text = message.content[0].text.strip()
        
        # Remove markdown code blocks if present
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
            response_text = response_text.strip()
        
        recommendations_data = json.loads(response_text)
        
        # Convert to Pydantic models
        recommendations = [
            CostRecommendation(**rec)
            for rec in recommendations_data
        ]
        
        log.info("cost_recommendations_generated",
                org_id=usage.organization_id,
                count=len(recommendations))
        
        return recommendations
        
    except json.JSONDecodeError as e:
        log.error("claude_response_parse_failed",
                 error=str(e),
                 response=response_text[:500])
        return []
    
    except Exception as e:
        log.error("cost_optimizer_claude_failed",
                 org_id=usage.organization_id,
                 error=str(e),
                 exc_info=True)
        return []


async def _organization_of(user_id: str) -> str:
    """The caller's organization, from their membership.

    A subscription belongs to a user and has no organization_id column: asking
    for one made PostgREST answer 400 and these routes 500 for everyone.
    """
    res = await resources.http_client.get(
        f"{SUPABASE_URL}/rest/v1/organization_members",
        params={
            "select": "organization_id",
            "user_id": f"eq.{user_id}",
            "status": "eq.active",
            "limit": "1"
        },
        headers=get_supabase_headers()
    )
    res.raise_for_status()
    members = res.json()
    if not members:
        raise HTTPException(status_code=403, detail="User not in any organization")
    return members[0]["organization_id"]


# ============================================================================
# API ENDPOINTS
# ============================================================================

@router.get("/recommendations", response_model=List[CostRecommendation])
async def get_cost_recommendations(
    days: int = 30,
    user_id: str = Depends(get_current_user_id)
):
    """
    Get AI-powered cost optimization recommendations.
    
    **How it works:**
    1. Analyzes last 30 days of API usage
    2. Uses Claude 3.5 Sonnet to identify patterns
    3. Returns 3 actionable recommendations
    
    **Example Output:**
    ```json
    [
      {
        "action": "Switch 20% of GPT-4 to GPT-4 Turbo",
        "reasoning": "66% cheaper, same quality for simple tasks",
        "estimated_savings_usd": 400,
        "confidence": 0.85,
        "priority": 1,
        "implementation_steps": ["Identify simple requests", "Update model param"]
      }
    ]
    ```
    """
    try:
        org_id = await _organization_of(user_id)
        
        # Analyze usage patterns
        usage = await analyze_usage_patterns(org_id, days)
        
        if usage.total_requests == 0:
            return []
        
        # Generate recommendations with Claude
        recommendations = await generate_recommendations_with_claude(usage)
        
        # Sort by priority
        recommendations.sort(key=lambda r: r.priority)
        
        return recommendations
        
    except HTTPException:
        raise
    
    except Exception as e:
        log.error("get_recommendations_failed",
                 user_id=user_id,
                 error=str(e),
                 exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate recommendations")


@router.get("/usage-analysis", response_model=UsageAnalysis)
async def get_usage_analysis(
    days: int = 30,
    user_id: str = Depends(get_current_user_id)
):
    """
    Get detailed usage analysis (raw data).
    
    Returns aggregated usage by model without AI recommendations.
    """
    try:
        org_id = await _organization_of(user_id)
        
        # Analyze usage
        usage = await analyze_usage_patterns(org_id, days)
        
        return usage
        
    except HTTPException:
        raise
    
    except Exception as e:
        log.error("get_usage_analysis_failed",
                 user_id=user_id,
                 error=str(e),
                 exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to analyze usage")

