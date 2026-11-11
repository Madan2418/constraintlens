"""``GET /v1/traces/{id}/attribution``, attribution at close and in replay (§11.1, ADR 0009)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from constraintlens.sdk import next_envelope
from tests.factories import T0
from tests.service_helpers import build_app, http_client
from tests.unit.test_api import open_trace

if TYPE_CHECKING:
    from constraintlens.domain.models import Envelope


def checkpoint(env: Envelope, hop: str, checkpoint: str, text: str, **extra: Any) -> dict[str, Any]:
    emitted = next_envelope(env, hop_id=hop, agent_id=f"agent-{hop}", text=text, emitted_at=T0)
    return {
        "trace_id": str(env.trace_id),
        "hop_id": hop,
        "checkpoint": checkpoint,
        "envelope": emitted.model_dump(mode="json"),
        **extra,
    }


async def drifted_trace(c: Any) -> str:
    tid, root = await open_trace(c)
    ok = await c.post(f"/v1/traces/{tid}/checkpoints",
                      json=checkpoint(root, "h1", "PUBLISH", "Refund up to $100."))  # fmt: skip
    assert ok.json()["decision"] == "ALLOW"
    h1 = next_envelope(root, hop_id="h1", agent_id="agent-h1", text="Refund up to $100.",
                       emitted_at=T0)  # fmt: skip
    bad = await c.post(f"/v1/traces/{tid}/checkpoints",
                       json=checkpoint(h1, "h2", "PUBLISH", "Refund in full."))  # fmt: skip
    assert bad.json()["decision"] in {"RE_GROUND", "BLOCK"}
    return tid


async def test_attribution_endpoint_live_then_recorded_at_close() -> None:
    app, _ = await build_app(drift_tier=True)
    async with http_client(app) as c:
        tid = await drifted_trace(c)
        live = (await c.get(f"/v1/traces/{tid}/attribution")).json()
        assert live["status"] == "open"
        assert live["recorded"] == []
        [a] = live["attributions"]
        assert (a["culprit_hop"], a["culprit_agent"], a["cause"]) == ("h2", "agent-h2", "REWRITE")
        assert live["params"] == {"theta": 0.35, "delta_min": 0.15, "dwell_threshold_s": 300.0}

        closed = (await c.post(f"/v1/traces/{tid}/close")).json()
        assert [x["culprit_hop"] for x in closed["attributions"]] == ["h2"]
        after = (await c.get(f"/v1/traces/{tid}/attribution")).json()
        assert after["recorded"] == after["attributions"]
        types = [e["type"] for e in (await c.get(f"/v1/traces/{tid}/events")).json()]
        assert types[-2:] == ["ATTRIBUTION_COMPUTED", "TRACE_CLOSED"]


async def test_attribution_parameters_and_errors() -> None:
    app, _ = await build_app(drift_tier=True, theta_low=0.3, delta_min=0.2)
    async with http_client(app) as c:
        tid = await drifted_trace(c)
        body = (await c.get(f"/v1/traces/{tid}/attribution")).json()
        assert body["params"]["theta"] == 0.3
        assert body["params"]["delta_min"] == 0.2
        strict = (await c.get(f"/v1/traces/{tid}/attribution", params={"delta_min": 1.0})).json()
        assert strict["params"]["delta_min"] == 1.0
        assert strict["attributions"][0]["culprit_hop"] is None  # no drop exceeds 1.0
        bad = await c.get(f"/v1/traces/{tid}/attribution", params={"theta": 2})
        assert bad.status_code == 400  # RFC 7807 InvalidEnvelope (§11.4)
        missing = await c.get("/v1/traces/00000000-0000-0000-0000-000000000009/attribution")
        assert missing.status_code == 404


async def test_replay_reports_original_and_replayed_attribution() -> None:
    app, _ = await build_app(drift_tier=True)
    async with http_client(app) as c:
        tid = await drifted_trace(c)
        same = (await c.post(f"/v1/traces/{tid}/replay")).json()["attribution"]
        # A replayed verdict is not an event: its evidence cites only the checkpoint.
        strip = [{k: v for k, v in a.items() if k != "evidence_events"} for a in same["original"]]
        assert strip == [
            {k: v for k, v in a.items() if k != "evidence_events"} for a in same["replayed"]
        ]
        assert set(same["replayed"][0]["evidence_events"]) < set(
            same["original"][0]["evidence_events"]
        )
        assert [a["culprit_hop"] for a in same["original"]] == ["h2"]
        ablated = (await c.post(f"/v1/traces/{tid}/replay", json={"tiers": ["T0", "T1a"]})).json()
        assert ablated["attribution"]["replayed"] == []  # without the drift tier nothing failed
        tuned = await c.post(f"/v1/traces/{tid}/replay", json={"delta_min": 0.5})
        assert tuned.json()["attribution"]["params"]["delta_min"] == 0.5
