"""
OXY ENGINE - BUDGET ALERT SYSTEM
=================================
Monitors project budgets and sends email alerts at 80%, 95%, and 100% thresholds.

Features:
- Real-time budget monitoring
- Email notifications via SendGrid/Resend
- Rate limiting (1 email per threshold per day)
- Configurable thresholds

Author: Oxy contributors
"""

import os
from datetime import datetime, timezone, timedelta
import inspect
from decimal import Decimal

# Import from core.py instead of main.py to break circular dependency;
# use the resources container to avoid None binding issues
from core import log, resources, SITE_URL
from core.notify_channels import send_slack_discord_alert
from core.zapier import send_zapier_event

# Budget alert thresholds (percentage of budget_limit)
ALERT_THRESHOLDS = [80, 95, 100]

# Feature / email configuration (env re-read at runtime for tests/overrides)
BUDGET_ALERTS_ENABLED = os.getenv("BUDGET_ALERTS_ENABLED", "true").lower() == "true"
EMAIL_FROM = os.getenv("EMAIL_FROM", "alerts@example.com")
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").lower() == "true"

# Resend API configuration
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_API_URL = "https://api.resend.com/emails"


async def _maybe_await(result):
    """Helper to support sync or async mocks/clients."""
    if inspect.isawaitable(result):
        return await result
    return result


def _alerts_enabled() -> bool:
    return os.getenv("BUDGET_ALERTS_ENABLED", "true").lower() == "true"


def _email_enabled() -> bool:
    return os.getenv("EMAIL_ENABLED", "false").lower() == "true"


async def check_budget_and_alert(
    project_id: str,
    current_spend: Decimal,
    budget_limit: Decimal,
    user_id: str,
    organization_id: str | None = None,
):
    """
    Check if current spend crosses alert thresholds and send emails.
    
    Args:
        project_id: Project UUID
        current_spend: Current spending amount
        budget_limit: Budget limit for project
        user_id: User UUID for email lookup
    
    Returns:
        None (sends emails as side effect)
    """
    if not _alerts_enabled():
        log.warning("budget_alerts_disabled", project_id=project_id)
        return

    if budget_limit <= 0:
        log.info("budget_alert_skipped_unlimited_budget", project_id=project_id, budget_limit=float(budget_limit))
        return  # No budget limit set, skip alerts

    if not _email_enabled():
        log.warning("budget_alert_email_disabled", project_id=project_id)
    
    # Calculate percentage
    percentage = (current_spend / budget_limit) * 100
    
    # Check each threshold
    for threshold in ALERT_THRESHOLDS:
        if percentage >= threshold:
            log.info("budget_alert_threshold_reached",
                     project_id=project_id,
                     user_id=user_id,
                     threshold=threshold,
                     percentage=float(percentage))
            await send_budget_alert_if_needed(
                project_id=project_id,
                user_id=user_id,
                threshold=threshold,
                current_spend=current_spend,
                budget_limit=budget_limit,
                percentage=percentage,
                organization_id=organization_id,
            )


async def send_budget_alert_if_needed(
    project_id: str,
    user_id: str,
    threshold: int,
    current_spend: Decimal,
    budget_limit: Decimal,
    percentage: float,
    organization_id: str | None = None,
):
    """
    Send budget alert email if not already sent today for this threshold.
    
    Rate limiting: Max 1 email per threshold per day per project.
    """
    # Check if alert was already sent today
    alert_key = f"budget_alert:{project_id}:{threshold}"
    
    try:
        # Check Redis for recent alert
        last_alert = await resources.redis.get(alert_key)
        if last_alert:
            log.debug("budget_alert_rate_limited", 
                     project_id=project_id, 
                     threshold=threshold,
                     reason="Alert already sent today")
            return
    except Exception as redis_error:
        log.warning("budget_alert_redis_check_failed", error=str(redis_error))
        # Continue - better to send duplicate than miss alert
    
    # Get user email from database
    try:
        user_result = await _maybe_await(
            resources.supabase.table("profiles").select("email, full_name").eq("id", user_id).single().execute()
        )
        
        if not user_result.data:
            log.error("budget_alert_user_not_found", user_id=user_id)
            return
        
        user_email = user_result.data.get("email")
        user_name = user_result.data.get("full_name", "User")
        
        if not user_email:
            log.error("budget_alert_no_email", user_id=user_id)
    except Exception as db_error:
        log.error("budget_alert_db_error", user_id=user_id, exc_info=True)
        # Fallbacks: best effort send if we somehow have an email later (unlikely without DB)
        user_email = None
        user_name = "User"
    
    # Get project name
    try:
        project_result = await _maybe_await(
            resources.supabase.table("projects").select("name").eq("id", project_id).single().execute()
        )
        project_name = project_result.data.get("name", "Your Project") if project_result.data else "Your Project"
    except Exception:
        project_name = "Your Project"
    
    channel_sent = False

    # Send email notification when configured
    try:
        if _email_enabled() and user_email:
            sent = await send_budget_alert_email(
                to_email=user_email,
                to_name=user_name,
                project_name=project_name,
                threshold=threshold,
                current_spend=current_spend,
                budget_limit=budget_limit,
                percentage=percentage
            )
            channel_sent = channel_sent or bool(sent)
        elif _email_enabled() and not user_email:
            log.warning("budget_alert_no_recipient_email", project_id=project_id, user_id=user_id)

    except Exception as email_error:
        log.error("budget_alert_email_failed", 
                 project_id=project_id, 
                 threshold=threshold, 
                 exc_info=True)

    # Slack/Discord notifications (best-effort, do not block)
    if organization_id:
        try:
            text = (
                f"[Budget Alert] Project {project_name} reached {int(percentage)}% of its budget "
                f"(${float(current_spend):.2f}/${float(budget_limit):.2f}) at threshold {threshold}%."
            )
            sent = await send_slack_discord_alert(organization_id, text)
            channel_sent = channel_sent or bool(sent)
        except Exception as exc:
            log.error("budget_alert_slack_discord_failed", project_id=project_id, org_id=organization_id, error=str(exc)[:200])

        try:
            zapier_payload = {
                "project_id": project_id,
                "project_name": project_name,
                "threshold": threshold,
                "current_spend": float(current_spend),
                "budget_limit": float(budget_limit),
                "percentage": float(percentage),
            }
            sent = await send_zapier_event(
                organization_id,
                "budget.threshold_crossed",
                zapier_payload,
            )
            channel_sent = channel_sent or sent
        except Exception as exc:
            log.error("budget_alert_zapier_failed", project_id=project_id, org_id=organization_id, error=str(exc)[:200])

    # Mark alert as sent (24h TTL) if any channel delivered
    if channel_sent:
        try:
            await resources.redis.set(alert_key, datetime.now(timezone.utc).isoformat(), ex=86400)
        except Exception as redis_error:
            log.warning("budget_alert_redis_set_failed", error=str(redis_error))
        
        log.info(
            "budget_alert_sent",
            project_id=project_id,
            user_id=user_id,
            threshold=threshold,
            current_spend=float(current_spend),
            budget_limit=float(budget_limit),
        )


async def send_budget_alert_email(
    to_email: str,
    to_name: str,
    project_name: str,
    threshold: int,
    current_spend: Decimal,
    budget_limit: Decimal,
    percentage: float
):
    """
    Send budget alert email via SendGrid.
    
    Warns the project owner before requests are blocked by the budget.
    """
    
    # Determine urgency level
    if threshold >= 100:
        urgency = "CRITICAL"
        subject = f"🚨 Budget Exceeded: {project_name}"
        message = f"Your project '{project_name}' has exceeded its budget limit."
        cta = "Increase Budget Now"
    elif threshold >= 95:
        urgency = "URGENT"
        subject = f"⚠️ Budget Alert: {project_name} at {int(percentage)}%"
        message = f"Your project '{project_name}' has reached {int(percentage)}% of its budget."
        cta = "Review Budget"
    else:
        urgency = "WARNING"
        subject = f"💡 Budget Notice: {project_name} at {int(percentage)}%"
        message = f"Your project '{project_name}' has used {int(percentage)}% of its budget."
        cta = "View Dashboard"
    
    # Get SendGrid API key
    SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
    if not SENDGRID_API_KEY:
        log.warning("sendgrid_not_configured", 
                   message="SENDGRID_API_KEY not set, cannot send budget alerts",
                   project_name=project_name,
                   threshold=threshold)
        return False
    
    # Construct HTML email (professional design)
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }}
            .content {{ background: white; padding: 30px; border: 1px solid #e1e4e8; border-top: none; }}
            .alert-box {{ background: {"#fee" if threshold >= 100 else "#fef8e7" if threshold >= 95 else "#e8f4f8"}; 
                         border-left: 4px solid {"#e53e3e" if threshold >= 100 else "#f59e0b" if threshold >= 95 else "#3182ce"}; 
                         padding: 15px; margin: 20px 0; border-radius: 4px; }}
            .stats {{ background: #f6f8fa; padding: 20px; border-radius: 6px; margin: 20px 0; }}
            .stat-row {{ display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #e1e4e8; }}
            .stat-row:last-child {{ border-bottom: none; }}
            .button {{ display: inline-block; background: #667eea; color: white !important; padding: 12px 30px; text-decoration: none; border-radius: 6px; font-weight: 600; margin: 20px 0; }}
            .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 style="margin: 0; font-size: 24px;">Budget Alert</h1>
                <p style="margin: 10px 0 0 0; opacity: 0.9;">Oxy Engine - AI Gateway</p>
            </div>
            <div class="content">
                <p>Hi {to_name},</p>
                
                <div class="alert-box">
                    <strong>{message}</strong>
                </div>
                
                <div class="stats">
                    <div class="stat-row">
                        <span><strong>Project:</strong></span>
                        <span>{project_name}</span>
                    </div>
                    <div class="stat-row">
                        <span><strong>Current Spend:</strong></span>
                        <span>${current_spend:.2f}</span>
                    </div>
                    <div class="stat-row">
                        <span><strong>Budget Limit:</strong></span>
                        <span>${budget_limit:.2f}</span>
                    </div>
                    <div class="stat-row">
                        <span><strong>Usage:</strong></span>
                        <span style="color: {"#e53e3e" if threshold >= 100 else "#f59e0b" if threshold >= 95 else "#3182ce"}; font-weight: 600;">
                            {percentage:.1f}%
                        </span>
                    </div>
                </div>
                
                <p>
                    {"<strong>⚠️ Your API requests are now blocked until you increase the budget limit.</strong>" if threshold >= 100 else "Please consider increasing your budget limit to avoid service interruption."}
                </p>
                
                <center>
                    <a href="{SITE_URL.rstrip('/')}/dashboard" class="button">{cta}</a>
                </center>
                
                <p style="color: #666; font-size: 14px; margin-top: 30px;">
                    Need help? Contact the operator of this Oxy gateway.
                </p>
            </div>
            <div class="footer">
                <p>Oxy LLM gateway</p>
                <p>This is an automated notification. You're receiving this because you're the owner of this project.</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    # Plain text fallback
    text_body = f"""
Hi {to_name},

{message}

Details:
- Project: {project_name}
- Current Spend: ${current_spend:.2f}
- Budget Limit: ${budget_limit:.2f}
- Usage: {percentage:.1f}%

{"Your API requests are now blocked until you increase the budget limit." if threshold >= 100 else "Please consider increasing your budget limit to avoid service interruption."}

Manage your budget: {SITE_URL.rstrip('/')}/dashboard

-- Oxy
"""
    
    # Send via SendGrid
    try:
        response = await resources.http_client.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "personalizations": [{
                    "to": [{"email": to_email, "name": to_name}],
                    "subject": subject
                }],
                "from": {
                    "email": EMAIL_FROM,
                    "name": "Oxy Engine Alerts"
                },
                "content": [
                    {"type": "text/plain", "value": text_body},
                    {"type": "text/html", "value": html_body}
                ],
                "categories": ["budget_alert", urgency.lower()],
                "tracking_settings": {
                    "click_tracking": {"enable": True},
                    "open_tracking": {"enable": True}
                }
            },
            timeout=10.0
        )
        
        if response.status_code == 202:
            log.info("budget_alert_email_sent", 
                    email=to_email,
                    project=project_name,
                    threshold=threshold,
                    urgency=urgency,
                    message="Budget alert email sent successfully via SendGrid")
            return True
        else:
            log.error("sendgrid_budget_alert_error", 
                     status_code=response.status_code,
                     response=response.text,
                     email=to_email)
            return False
            
    except Exception as e:
        log.error("budget_alert_email_failed", 
                 email=to_email, 
                 error=str(e),
                 exc_info=True,
                 message="Failed to send budget alert email")
        return False


# Example usage:
# await check_budget_and_alert(
#     project_id="uuid",
#     current_spend=Decimal("85.00"),
#     budget_limit=Decimal("100.00"),
#     user_id="uuid"
# )

