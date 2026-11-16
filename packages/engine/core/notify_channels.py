"""
Slack/Discord notification helpers (best-effort, outbound only).
"""

from __future__ import annotations

import asyncio
from typing import Dict, Any, Callable
from urllib.parse import urlparse

import httpx

from core import SUPABASE_URL, resources, log
from dependencies import get_supabase_headers

SLACK_PREFIX = "https://hooks.slack.com/"
DISCORD_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
WEBHOOK_TIMEOUT = 5.0
MAX_RETRIES = 1  # one retry for transient failures
BACKOFF_SECONDS = 0.5

PostFn = Callable[[str, Dict[str, Any]], Any]


def _safe_host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).netloc
    except Exception:
        return None


async def _default_post(url: str, payload: Dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(url, json=payload, timeout=WEBHOOK_TIMEOUT)


async def _post_webhook(
    url: str,
    payload: Dict[str, Any],
    *,
    post: PostFn | None = None,
    max_retries: int = MAX_RETRIES,
    backoff_seconds: float = BACKOFF_SECONDS,
) -> bool:
    post_fn = post or _default_post
    attempt = 0
    host = _safe_host(url)
    while True:
        try:
            resp = await post_fn(url, payload)
            status = getattr(resp, "status_code", None)
            if status is None:
                return True
            if status >= 500:
                if attempt < max_retries:
                    attempt += 1
                    await asyncio.sleep(backoff_seconds * attempt)
                    continue
                log.error("webhook_send_failed", host=host, status_code=status)
                return False
            if status >= 400:
                log.warning("webhook_send_rejected", host=host, status_code=status)
                return False
            return True
        except Exception as exc:
            if attempt < max_retries:
                attempt += 1
                await asyncio.sleep(backoff_seconds * attempt)
                continue
            log.error("webhook_send_exception", host=host, error=str(exc)[:200])
            return False


class SlackNotifier:
    def __init__(self, slack_webhook_url: str, post: PostFn | None = None):
        self.url = slack_webhook_url
        self._post = post

    async def send(self, text: str) -> bool:
        if not self.url or not self.url.startswith(SLACK_PREFIX):
            return False
        payload = {"text": text}
        return await _post_webhook(self.url, payload, post=self._post)


class DiscordNotifier:
    def __init__(self, discord_webhook_url: str, post: PostFn | None = None):
        self.url = discord_webhook_url
        self._post = post

    async def send(self, text: str) -> bool:
        if not self.url or not any(self.url.startswith(p) for p in DISCORD_PREFIXES):
            return False
        payload = {"content": text}
        return await _post_webhook(self.url, payload, post=self._post)


async def _fetch_channel_settings(org_id: str) -> Dict[str, Any]:
    try:
        res = await resources.http_client.get(
            f"{SUPABASE_URL}/rest/v1/notification_settings",
            params={
                "organization_id": f"eq.{org_id}",
                "select": "slack_webhook_url,slack_enabled,discord_webhook_url,discord_enabled",
                "limit": 1,
            },
            headers=get_supabase_headers(),
            timeout=WEBHOOK_TIMEOUT,
        )
        res.raise_for_status()
        rows = res.json() or []
        return rows[0] if rows else {}
    except Exception as exc:
        log.error("notify_channel_fetch_failed", org_id=org_id, error=str(exc)[:200])
        return {}


async def send_slack_discord_alert(org_id: str, text: str) -> bool:
    """Post to the organization's enabled Slack and Discord webhooks; True when
    at least one of them accepted the message."""
    settings = await _fetch_channel_settings(org_id)
    slack_url = settings.get("slack_webhook_url") if settings.get("slack_enabled") else None
    discord_url = settings.get("discord_webhook_url") if settings.get("discord_enabled") else None

    notifiers = []
    if slack_url and slack_url.startswith(SLACK_PREFIX):
        notifiers.append(SlackNotifier(slack_url))
    if discord_url and any(discord_url.startswith(p) for p in DISCORD_PREFIXES):
        notifiers.append(DiscordNotifier(discord_url))

    delivered = False
    for notifier in notifiers:
        try:
            delivered = bool(await notifier.send(text)) or delivered
        except Exception as exc:
            # Best-effort: log, do not raise
            log.error("notify_channel_send_failed", org_id=org_id, channel=notifier.__class__.__name__, error=str(exc)[:200])
    return delivered
