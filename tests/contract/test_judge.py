"""Judge contract (§6.3, §7.4): the scripted fake and both real adapters.

The real adapters run against fakes of their transport (no network, no spend):
``AnthropicJudge`` gets a stand-in SDK client, ``OpenAICompatJudge`` an
``httpx.MockTransport``. Live calls are opt-in (``CL_JUDGE_LIVE=1`` plus a key).
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from constraintlens.adapters.judge.anthropic import AnthropicJudge
from constraintlens.adapters.judge.openai_compat import OpenAICompatJudge
from constraintlens.adapters.judge.scripted import ScriptedJudge
from constraintlens.domain.models import JudgeResult, ProposedAction
from constraintlens.domain.ports import Judge

if TYPE_CHECKING:
    from collections.abc import Callable

VALID = json.dumps(
    {"verdict": "CONTRADICTED", "reason": "the limit is gone", "confidence": 0.9,
     "evidence": None}
)  # fmt: skip


class FakeMessages:
    """Stands in for ``client.messages`` / ``client.beta.messages`` of the anthropic SDK."""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def message(text: str, stop_reason: str = "end_turn", category: str | None = None) -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if category else None,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
    )


def fake_anthropic_client(*replies: Any) -> Any:
    shared = FakeMessages(list(replies))
    return SimpleNamespace(messages=shared, beta=SimpleNamespace(messages=shared))


def openai_transport(content: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handler)


def _anthropic() -> Judge:
    return AnthropicJudge(client=fake_anthropic_client(message(VALID)))


def _openai() -> Judge:
    return OpenAICompatJudge(
        base_url="http://llm.test/v1", model="local-model", transport=openai_transport(VALID)
    )


FACTORIES: list[Callable[[], Judge]] = [ScriptedJudge, _anthropic, _openai]
if os.environ.get("CL_JUDGE_LIVE") and os.environ.get("CL_JUDGE_API_KEY"):  # opt-in, costs money

    def _anthropic_live() -> Judge:
        return AnthropicJudge(api_key=os.environ["CL_JUDGE_API_KEY"])

    FACTORIES.append(_anthropic_live)


@pytest.fixture(params=FACTORIES, ids=lambda f: f.__name__.lstrip("_"))
def judge(request: pytest.FixtureRequest) -> Judge:
    return request.param()


def test_satisfies_protocol(judge: Judge) -> None:
    assert isinstance(judge, Judge)


def test_exposes_cache_key_identity(judge: Judge) -> None:
    assert judge.model_id
    assert judge.prompt_version


@pytest.mark.parametrize("action", [None, ProposedAction(tool="issue_refund", args={"amount": 5})])
async def test_returns_valid_result(judge: Judge, action: ProposedAction | None) -> None:
    result = await judge.judge(
        canonical_text="Refund up to 100 USD.",
        task_text="Refund the customer in full.",
        proposed_action=action,
    )
    assert isinstance(result, JudgeResult)
    assert result.verdict in {"ENTAILED", "CONTRADICTED", "AMBIGUOUS"}
    assert result.reason
    assert 0.0 <= result.confidence <= 1.0


async def test_extract_constraints_returns_text(judge: Judge) -> None:
    from datetime import UTC, datetime

    raw = await judge.extract_constraints(
        instruction="Refund up to $100.", issued_at=datetime(2026, 11, 7, tzinfo=UTC)
    )
    assert isinstance(raw, str)
