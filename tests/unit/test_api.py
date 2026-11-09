"""FastAPI delivery layer (§11): endpoints, RFC 7807 errors, auth, size limit."""

from __future__ import annotations

from typing import Any

from constraintlens.domain.models import Envelope
from constraintlens.sdk import next_envelope
from tests.factories import T0
from tests.service_helpers import WORKED, build_app, http_client

BINDING = {"action": "refund", "resource": "order", "amount_arg": "amount", "unit": "USD"}


def tool_body(env: Envelope, amount: int, hop: str = "h3.t1") -> dict[str, Any]:
    acting = next_envelope(env, hop_id="h3", agent_id="payments", text="Refund.", emitted_at=T0)
    return {
        "trace_id": str(env.trace_id),
        "hop_id": hop,
        "checkpoint": "TOOL_CALL",
        "envelope": acting.model_dump(mode="json"),
        "proposed_action": {"tool": "issue_refund", "args": {"amount": amount}, "binding": BINDING},
    }


async def open_trace(c: Any, **extra: Any) -> tuple[str, Envelope]:
    r = await c.post("/v1/traces", json={"instruction": WORKED, "owner": "user:a", **extra})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["extraction"]["sources"] == ["rule"]
    return body["trace_id"], Envelope.model_validate(body["envelope"])


async def test_full_flow() -> None:
    app, _ = await build_app()
    async with http_client(app) as c:
        tid, env = await open_trace(c)
        r = await c.post(f"/v1/traces/{tid}/checkpoints", json=tool_body(env, 2000))
        assert r.status_code == 200
        assert r.json()["decision"] == "BLOCK"
        same = await c.post(f"/v1/traces/{tid}/checkpoints", json=tool_body(env, 2000),
                            headers={"Idempotency-Key": "k-1"})  # fmt: skip
        assert same.json()["verdict_id"] == r.json()["verdict_id"]

        trace = (await c.get(f"/v1/traces/{tid}")).json()
        assert (trace["status"], trace["outcome"]) == ("open", "OPEN")
        events = (await c.get(f"/v1/traces/{tid}/events")).json()
        later = (
            await c.get(
                f"/v1/traces/{tid}/events", params={"after_event_id": events[2]["event_id"]}
            )
        ).json()
        assert len(later) == len(events) - 3

        replay = (await c.post(f"/v1/traces/{tid}/replay")).json()
        assert replay["changed"] == 0
        assert replay["tiers"] == ["T0", "T1a"]
        ablated = (await c.post(f"/v1/traces/{tid}/replay", json={"tiers": ["T0"]})).json()
        assert ablated["items"][0]["replayed_decision"] == "ALLOW"
        relaxed = await c.post(f"/v1/traces/{tid}/replay", json={"escalate_approvals": True,
                                                                 "max_regrounds": 0})  # fmt: skip
        assert relaxed.json()["items"][0]["replayed_decision"] == "ESCALATE"

        closed = (await c.post(f"/v1/traces/{tid}/close")).json()
        assert (closed["status"], closed["outcome"]) == ("closed", "BLOCKED")
        metrics = (await c.get("/v1/metrics")).json()
        assert metrics["decisions"] == {"BLOCK": 1}
        health = (await c.get("/v1/health")).json()
        assert health["status"] == "ok"
        assert health["tiers"] == ["T0", "T1a"]


async def test_declared_org_policy_and_conflicts() -> None:
    app, _ = await build_app()
    org = {"scope": "org", "owner": "org:acme", "origin": "policy", "action": "refund",
           "resource": "order", "limit": {"max_value": "50", "unit": "USD"}}  # fmt: skip
    async with http_client(app) as c:
        r = await c.post(
            "/v1/traces", json={"instruction": WORKED, "owner": "u", "declared": [org]}
        )
        assert [x["kind"] for x in r.json()["conflicts"]] == ["SUPERSEDED"]
        assert len(r.json()["envelope"]["constraints"]) == 2


async def test_problem_details() -> None:
    app, _ = await build_app()
    async with http_client(app) as c:
        tid, env = await open_trace(c)
        unknown = await c.get("/v1/traces/00000000-0000-4000-8000-000000000000")
        assert unknown.status_code == 404
        assert unknown.headers["content-type"] == "application/problem+json"
        assert unknown.json()["type"].endswith("UnknownTrace")
        assert (
            await c.get("/v1/traces/00000000-0000-4000-8000-000000000000/events")
        ).status_code == 404

        bad = await c.post(f"/v1/traces/{tid}/checkpoints", json={"nope": 1})
        assert bad.status_code == 400
        assert bad.json()["title"] == "Invalid envelope"
        assert bad.json()["errors"]

        body = tool_body(env, 1)
        mismatch = await c.post(
            "/v1/traces/00000000-0000-4000-8000-000000000001/checkpoints", json=body
        )
        assert mismatch.status_code == 400
        assert "differ" in mismatch.json()["detail"]

        tool = tool_body(env, 1)
        tool["proposed_action"] = None
        assert (await c.post(f"/v1/traces/{tid}/checkpoints", json=tool)).status_code == 400

        bad_tier = await c.post(f"/v1/traces/{tid}/replay", json={"tiers": ["T2"]})
        assert bad_tier.status_code == 400
        assert "not available" in bad_tier.json()["detail"]

        await c.post(f"/v1/traces/{tid}/close")
        closed = await c.post(f"/v1/traces/{tid}/checkpoints", json=tool_body(env, 1, hop="h4.t1"))
        assert closed.status_code == 400

        invalid_limit = {"scope": "org", "owner": "o", "origin": "policy", "action": "refund",
                         "resource": "order", "limit": {"max_value": "10", "min_value": "200",
                                                        "unit": "USD"}}  # fmt: skip
        rejected = await c.post("/v1/traces", json={"instruction": "Refund it.", "owner": "u",
                                                    "declared": [invalid_limit]})  # fmt: skip
        assert rejected.status_code == 400  # malformed policy: rejected at the boundary


async def test_unsatisfiable_is_409() -> None:
    app, _ = await build_app()
    org = {"scope": "org", "owner": "o", "origin": "policy", "action": "refund",
           "resource": "order", "subject": "order:9"}  # fmt: skip
    async with http_client(app) as c:
        r = await c.post("/v1/traces", json={"instruction": "Refund order #4471 up to $100.",
                                             "owner": "u", "declared": [org]})  # fmt: skip
        assert r.status_code == 409
        assert r.json()["title"] == "Unsatisfiable constraints"


async def test_api_key_and_size_limit() -> None:
    app, _ = await build_app(api_key="s3cret", max_request_bytes=2000)
    async with http_client(app) as anon:
        assert (await anon.get("/v1/health")).status_code == 200  # probes stay open
        denied = await anon.post("/v1/traces", json={"instruction": WORKED, "owner": "u"})
        assert denied.status_code == 401
        assert (await anon.get("/v1/metrics")).status_code == 401
    async with http_client(app, **{"X-API-Key": "wrong"}) as wrong:
        assert (
            await wrong.post("/v1/traces", json={"instruction": WORKED, "owner": "u"})
        ).status_code == 401
    async with http_client(app, **{"X-API-Key": "s3cret"}) as ok:
        assert (
            await ok.post("/v1/traces", json={"instruction": WORKED, "owner": "u"})
        ).status_code == 201
        huge = await ok.post("/v1/traces", json={"instruction": "x" * 5000, "owner": "u"})
        assert huge.status_code == 413


async def test_production_startup_builds_sqlite_store(tmp_path: Any) -> None:
    from constraintlens.adapters.store.sqlite import SQLiteEventStore
    from constraintlens.api.app import create_app
    from tests.service_helpers import settings

    db = tmp_path / "nested" / "cl.db"
    app = create_app(settings(db_url=f"sqlite:///{db}"))
    async with app.router.lifespan_context(app), http_client(app) as c:
        assert isinstance(app.state.container.store, SQLiteEventStore)
        tid, _ = await open_trace(c)
        assert (await c.get(f"/v1/traces/{tid}")).status_code == 200
    assert db.exists()  # parent directory created, WAL database written
