"""Deleting an agent.

Up to 1.0.5, DELETE /v1/agents/{id}:
  * marked the agent's running sessions killed by agent id alone, so the owner of
    one organization could end another organization's sessions by sending that
    organization's agent id (the delete itself was scoped and found nothing, but
    the sessions were already killed);
  * left kill_requested_at and kill_requested_by empty and set no kill flag in
    Redis, which is what the gateway reads first;
  * answered 500 after deleting the agent, because the engine's database client
    did not ask PostgREST for the deleted rows and tried to read an empty body.
"""
import httpx
import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from fastapi import HTTPException

from core import resources
from main import SupabaseTable

ORG = "0b5c1d6e-0000-4000-8000-000000000001"
OTHER_ORG = "0b5c1d6e-0000-4000-8000-000000000002"
AGENT = "0b5c1d6e-0000-4000-8000-0000000000a1"
SESSION = "0b5c1d6e-0000-4000-8000-0000000000b1"
ENDED = "0b5c1d6e-0000-4000-8000-0000000000b2"


class Table:
    """One table with select/update/delete and eq/in_, as the engine's client chains them."""

    def __init__(self, rows):
        self.rows, self._filters, self._update, self._delete = rows, [], None, False

    def _reset(self):
        self._filters, self._update, self._delete = [], None, False
        return self

    def select(self, fields="*"):
        return self._reset()

    def update(self, data):
        self._reset()
        self._update = data
        return self

    def delete(self):
        self._reset()
        self._delete = True
        return self

    def eq(self, col, val):
        self._filters.append((col, [val]))
        return self

    def in_(self, col, vals):
        self._filters.append((col, list(vals)))
        return self

    async def execute(self):
        hit = [r for r in self.rows if all(r.get(c) in vs for c, vs in self._filters)]
        if self._update is not None:
            for r in hit:
                r.update(self._update)
        if self._delete:
            for r in hit:
                self.rows.remove(r)
        return type("R", (), {"data": [dict(r) for r in hit]})


class Db:
    def __init__(self):
        self.agents = [{"id": AGENT, "organization_id": ORG}]
        self.sessions = [
            {"id": SESSION, "agent_id": AGENT, "organization_id": ORG, "status": "running"},
            {"id": ENDED, "agent_id": AGENT, "organization_id": ORG, "status": "completed"},
        ]

    def table(self, name):
        return Table({"agents": self.agents, "agent_sessions": self.sessions}[name])


@pytest_asyncio.fixture
async def env():
    saved = {n: getattr(resources, n, None) for n in ("redis", "supabase")}
    redis = FakeAsyncRedis(decode_responses=True)
    resources.redis = redis
    resources.supabase = Db()
    try:
        yield redis
    finally:
        for n, v in saved.items():
            setattr(resources, n, v)
        await redis.flushall()


def owner(org=ORG):
    return {"organization_id": org, "user_id": "user-1", "role": "owner"}


@pytest.mark.asyncio
async def test_another_organizations_agent_is_refused_and_its_sessions_left_alone(env):
    from routes.agents import delete_agent
    with pytest.raises(HTTPException) as exc:
        await delete_agent(AGENT, auth=owner(OTHER_ORG))
    assert exc.value.status_code == 404
    assert [(s["id"], s["status"]) for s in resources.supabase.sessions] == [(SESSION, "running"), (ENDED, "completed")]
    assert resources.supabase.agents, "the agent is still there"
    assert not await env.exists("oxy:session:%s:terminated" % SESSION)


@pytest.mark.asyncio
async def test_deleting_an_agent_flags_its_running_sessions_for_the_gateway(env):
    from routes.agents import delete_agent
    resources.supabase.agents.append({"id": "keep", "organization_id": ORG})
    sessions = resources.supabase.sessions
    seen = {}
    real_execute = Table.execute

    async def spy(self):
        out = await real_execute(self)
        if self._update is not None:
            seen.update({r["id"]: r for r in out.data})
        return out

    Table.execute = spy
    try:
        out = await delete_agent(AGENT, auth=owner())
    finally:
        Table.execute = real_execute
    assert out["success"] is True
    assert [a["id"] for a in resources.supabase.agents] == ["keep"]
    # The running session was killed with who, when and why; the finished one was not touched.
    killed = seen[SESSION]
    assert killed["status"] == "killed" and killed["kill_reason"] == "Agent deleted"
    assert killed["kill_requested_by"] == "user-1" and killed["kill_requested_at"]
    assert ENDED not in seen and next(s for s in sessions if s["id"] == ENDED)["status"] == "completed"
    # And the gateway, which reads the flag first on every request, refuses it at once.
    assert await env.exists("oxy:session:%s:terminated" % SESSION)
    assert not await env.exists("oxy:session:%s:terminated" % ENDED)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_the_database_client_returns_the_rows_a_delete_removed():
    """PostgREST answers a bare 204 unless asked for the rows; the engine's client read the empty body as JSON."""
    def handler(request):
        if "return=representation" in request.headers.get("Prefer", ""):
            return httpx.Response(200, json=[{"id": AGENT}])
        return httpx.Response(204)

    async with _client(handler) as client:
        res = await SupabaseTable("http://db", "k", client, "agents").delete().eq("id", AGENT).execute()
    assert res.data == [{"id": AGENT}]


@pytest.mark.asyncio
async def test_a_delete_that_removed_nothing_has_no_rows_and_is_not_an_error():
    async with _client(lambda request: httpx.Response(200, json=[])) as client:
        res = await SupabaseTable("http://db", "k", client, "agents").delete().eq("id", AGENT).execute()
    assert res.data == []


# ----------------------------------------------------------------------------- the same shape in the MCP routes
@pytest.mark.asyncio
async def test_another_organizations_mcp_server_keeps_its_tools(monkeypatch):
    """DELETE /v1/mcp/servers/{id} deleted the server's tools by server id, before the organization was looked at."""
    from routes.mcp import delete_mcp_server
    tables = {"mcp_servers": [{"id": "srv", "organization_id": ORG}],
              "tools": [{"id": "t1", "mcp_server_id": "srv"}]}

    class Backend:
        def table(self, name):
            return Table(tables[name])

    saved = getattr(resources, "supabase", None)
    resources.supabase = Backend()
    try:
        with pytest.raises(HTTPException) as exc:
            await delete_mcp_server("srv", auth={"organization_id": OTHER_ORG})
        assert exc.value.status_code == 404
        assert tables["tools"] and tables["mcp_servers"], "nothing of it was deleted"
        out = await delete_mcp_server("srv", auth={"organization_id": ORG})
        assert out["success"] is True and not tables["tools"] and not tables["mcp_servers"]
    finally:
        resources.supabase = saved
