"""GET /cost-optimizer/recommendations and /usage-analysis find the caller's organization.

Up to 1.0.5 both asked the `subscriptions` table for an `organization_id`
column it has never had (a subscription belongs to a user), so PostgREST
answered 400 and the routes answered 500 for everyone. The organization now
comes from the caller's membership, like every other dashboard route.
"""
import pytest

from core import resources

ORG = "0b5c1d6e-0000-4000-8000-000000000001"
USER = "0b5c1d6e-0000-4000-8000-0000000000c1"


class Res:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)

    def json(self):
        return self._data


class Http:
    """PostgREST as far as these routes use it: `subscriptions` has no organization_id."""

    def __init__(self, members=None):
        self.members = [{"organization_id": ORG}] if members is None else members
        self.calls = []

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/subscriptions"):
            if "organization_id" in (params or {}).get("select", ""):
                return Res(400, {"message": "column subscriptions.organization_id does not exist"})
            return Res(200, [])
        if url.endswith("/organization_members"):
            return Res(200, self.members)
        raise AssertionError("unexpected request " + url)


@pytest.fixture
def optimizer(monkeypatch):
    from routes import cost_optimizer as c

    http = Http()
    monkeypatch.setattr(resources, "http_client", http)
    seen = []

    async def analyze(org_id, days=30):
        seen.append(org_id)
        return c.UsageAnalysis(total_requests=0, total_cost_usd=0.0, model_breakdown={}, period_days=days, organization_id=org_id)

    monkeypatch.setattr(c, "analyze_usage_patterns", analyze)
    return c, http, seen


@pytest.mark.asyncio
async def test_usage_analysis_uses_the_callers_organization(optimizer):
    c, http, seen = optimizer
    out = await c.get_usage_analysis(days=7, user_id=USER)
    assert out.organization_id == ORG and seen == [ORG]
    members = [p for url, p in http.calls if url.endswith("/organization_members")]
    assert members and members[0]["user_id"] == "eq." + USER and members[0]["status"] == "eq.active"
    assert not any(url.endswith("/subscriptions") for url, _ in http.calls)


@pytest.mark.asyncio
async def test_recommendations_use_the_callers_organization(optimizer):
    c, http, seen = optimizer
    out = await c.get_cost_recommendations(days=30, user_id=USER)
    assert out == [] and seen == [ORG], "no usage, no recommendations, and no call to the model"
    assert not any(url.endswith("/subscriptions") for url, _ in http.calls)


@pytest.mark.asyncio
async def test_a_user_in_no_organization_is_refused_not_a_500(optimizer):
    from fastapi import HTTPException
    c, http, seen = optimizer
    http.members = []
    for call in (c.get_usage_analysis(days=7, user_id=USER), c.get_cost_recommendations(days=7, user_id=USER)):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 403
    assert seen == []
