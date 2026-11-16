import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from workers import budget_alerts


class DummyTable:
    def __init__(self, data):
        self.data = data

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def single(self):
        return self

    async def execute(self):
        return SimpleNamespace(data=self.data)


class DummySupabase:
    def __init__(self):
        self.profile_data = {"email": "user@example.com", "full_name": "User"}
        self.project_data = {"name": "Demo Project"}

    def table(self, name: str):
        if name == "profiles":
            return DummyTable(self.profile_data)
        if name == "projects":
            return DummyTable(self.project_data)
        return DummyTable({})


class DummyRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        return True


@pytest.mark.asyncio
async def test_budget_alert_sends_slack_discord_when_enabled(monkeypatch):
    calls = []
    zap_calls = []

    async def fake_send(org_id, text):
        calls.append((org_id, text))

    async def fake_zapier(org_id, event_type, payload):
        zap_calls.append((org_id, event_type, payload))
        return True

    # Patch resources
    monkeypatch.setattr(budget_alerts.resources, "supabase", DummySupabase())
    monkeypatch.setattr(budget_alerts.resources, "redis", DummyRedis())
    monkeypatch.setattr(budget_alerts, "send_slack_discord_alert", fake_send)
    monkeypatch.setattr(budget_alerts, "send_zapier_event", fake_zapier)

    # Avoid email path for faster test
    monkeypatch.setattr(budget_alerts, "EMAIL_ENABLED", False)

    await budget_alerts.send_budget_alert_if_needed(
        project_id="proj-1",
        user_id="user-1",
        threshold=95,
        current_spend=Decimal("95"),
        budget_limit=Decimal("100"),
        percentage=95.0,
        organization_id="org-1",
    )

    assert calls, "Slack/Discord notifier should be invoked when organization_id is present"
    assert calls[0][0] == "org-1"
    assert "Budget Alert" in calls[0][1]
    assert zap_calls and zap_calls[0][1] == "budget.threshold_crossed"


# Up to 1.0.4 check_budget_and_alert, the function the request path calls, did not
# hand the organization on, so the Slack, Discord and Zapier branch never ran for a
# real request: the test above calls the inner function directly and missed it.
@pytest.mark.asyncio
async def test_the_request_path_hands_the_organization_to_the_channels(monkeypatch):
    calls = []
    zap_calls = []

    async def fake_send(org_id, text):
        calls.append(org_id)
        return True

    async def fake_zapier(org_id, event_type, payload):
        zap_calls.append((org_id, payload["threshold"]))
        return True

    monkeypatch.setattr(budget_alerts.resources, "supabase", DummySupabase())
    monkeypatch.setattr(budget_alerts.resources, "redis", DummyRedis())
    monkeypatch.setattr(budget_alerts, "send_slack_discord_alert", fake_send)
    monkeypatch.setattr(budget_alerts, "send_zapier_event", fake_zapier)
    monkeypatch.setenv("EMAIL_ENABLED", "false")

    await budget_alerts.check_budget_and_alert(
        "proj-1", Decimal("96"), Decimal("100"), "user-1", "org-1"
    )

    assert calls == ["org-1", "org-1"], "80% and 95% each reach Slack/Discord for the organization"
    assert zap_calls == [("org-1", 80), ("org-1", 95)]


@pytest.mark.asyncio
async def test_a_threshold_nothing_delivered_is_not_marked_sent(monkeypatch):
    # The 24-hour "already sent" mark is set only when a channel took the alert;
    # up to 1.0.4 the Slack/Discord call set it whatever happened.
    redis = DummyRedis()

    async def nothing_sent(org_id, text):
        return False

    async def zapier_off(org_id, event_type, payload):
        return False

    monkeypatch.setattr(budget_alerts.resources, "supabase", DummySupabase())
    monkeypatch.setattr(budget_alerts.resources, "redis", redis)
    monkeypatch.setattr(budget_alerts, "send_slack_discord_alert", nothing_sent)
    monkeypatch.setattr(budget_alerts, "send_zapier_event", zapier_off)
    monkeypatch.setenv("EMAIL_ENABLED", "false")

    await budget_alerts.send_budget_alert_if_needed(
        project_id="proj-1",
        user_id="user-1",
        threshold=95,
        current_spend=Decimal("96"),
        budget_limit=Decimal("100"),
        percentage=96.0,
        organization_id="org-1",
    )

    assert redis.store == {}, "no channel delivered, so the next request may try again"


@pytest.mark.asyncio
async def test_slack_and_discord_post_from_the_stored_settings(monkeypatch):
    from core import notify_channels

    posted = []

    async def fake_post(url, payload):
        posted.append((url, payload))
        return SimpleNamespace(status_code=200)

    async def settings(org_id):
        return {
            "slack_webhook_url": "https://hooks.slack.com/services/T000/B000/test",
            "slack_enabled": True,
            "discord_webhook_url": "https://discord.com/api/webhooks/1/test",
            "discord_enabled": False,
        }

    monkeypatch.setattr(notify_channels, "_fetch_channel_settings", settings)
    monkeypatch.setattr(notify_channels, "_default_post", fake_post)

    assert await notify_channels.send_slack_discord_alert("org-1", "[Budget Alert] x") is True
    assert posted == [("https://hooks.slack.com/services/T000/B000/test", {"text": "[Budget Alert] x"})]

    async def none_enabled(org_id):
        return {"slack_webhook_url": "https://hooks.slack.com/services/T000/B000/test", "slack_enabled": False}

    posted.clear()
    monkeypatch.setattr(notify_channels, "_fetch_channel_settings", none_enabled)
    assert await notify_channels.send_slack_discord_alert("org-1", "[Budget Alert] x") is False
    assert posted == []
