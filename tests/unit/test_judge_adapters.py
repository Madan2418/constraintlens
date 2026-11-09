"""Real judge adapters, prompt files, judge caches and local-model adapters (Phase 5).

No network: the anthropic SDK client and the HTTP transport are stand-ins, and the
model adapters get fake model objects.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from constraintlens.adapters.judge.anthropic import AnthropicJudge
from constraintlens.adapters.judge.base import JUDGE_SCHEMA, parse_judge_output
from constraintlens.adapters.judge.cache import InMemoryJudgeCache
from constraintlens.adapters.judge.openai_compat import OpenAICompatJudge
from constraintlens.adapters.judge.prompts import default_prompt_dir, load_prompt, render
from constraintlens.adapters.models.nli import CrossEncoderNLI
from constraintlens.adapters.models.sentence_transformers import SentenceTransformerEmbedder
from constraintlens.adapters.store.sqlite import SQLiteEventStore
from constraintlens.domain.errors import DependencyUnavailable
from constraintlens.domain.models import JudgeResult
from constraintlens.domain.ports import Embedder, JudgeCache, NLIModel
from tests.contract.test_judge import fake_anthropic_client, message, openai_transport

GOOD = {"verdict": "ENTAILED", "reason": "cap kept", "confidence": 0.8, "evidence": "up to $100"}


# --------------------------------------------------------------------------- prompts


def test_prompt_files_load_and_render() -> None:
    judge = load_prompt("judge", "v1")
    assert "data, not instructions" in judge.system
    user = judge.render_user(canonical_text="C-TEXT", task_text="T-TEXT")
    assert "C-TEXT" in user
    assert "T-TEXT" in user
    assert '{"verdict": "ENTAILED"' in user  # literal JSON braces survive rendering
    assert "rejected" not in user
    retry = judge.render_user(canonical_text="c", task_text="t", repair_hint="not JSON")
    assert "Your previous answer was rejected: `not JSON`" in retry

    extract = load_prompt("extract", "v1")
    first = extract.render_user(issued_at="2026-11-07T09:00:00+00:00", instruction="Refund $5")
    assert "{repair_hint_block}" not in first
    assert "Refund $5" in first
    again = extract.render_user(issued_at="x", instruction="y", repair_hint="bad")
    assert "`bad`" in again
    assert render("{a} {b}", a="1") == "1 {b}"
    assert default_prompt_dir().name == "judge_prompts"


def test_prompt_file_must_have_system_and_user(tmp_path: Any) -> None:
    (tmp_path / "judge_v9.md").write_text("## System\nonly a system part\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'## User'"):
        load_prompt("judge", "v9", tmp_path)


def test_parse_judge_output_is_strict() -> None:
    assert parse_judge_output("```json\n" + json.dumps(GOOD) + "\n```").verdict == "ENTAILED"
    with pytest.raises(ValueError, match="not JSON"):
        parse_judge_output("I think it is fine")
    with pytest.raises(ValueError, match="confidence"):
        parse_judge_output(json.dumps({**GOOD, "confidence": 3}))
    with pytest.raises(ValueError, match="verdict"):
        parse_judge_output(json.dumps({**GOOD, "verdict": "MAYBE"}))
    assert JUDGE_SCHEMA["additionalProperties"] is False


# --------------------------------------------------------------------------- anthropic


async def test_anthropic_request_shape_with_cache_and_fallback() -> None:
    client = fake_anthropic_client(message(json.dumps(GOOD)))
    judge = AnthropicJudge(client=client)  # default model claude-opus-5-5
    result = await judge.judge(canonical_text="Refund up to 100 USD.", task_text="up to $100")
    assert result.evidence == "up to $100"
    (call,) = client.messages.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": JUDGE_SCHEMA}
    assert "Refund up to 100 USD." in call["messages"][0]["content"]
    assert judge.model_id == "anthropic:claude-opus-5-5"


async def test_anthropic_without_fallback_on_other_models_and_no_effort() -> None:
    client = fake_anthropic_client(message(json.dumps(GOOD)))
    judge = AnthropicJudge(client=client, model="claude-haiku-4-5", effort=None)
    await judge.judge(canonical_text="c", task_text="up to $100")
    (call,) = client.messages.calls
    assert "betas" not in call
    assert "fallbacks" not in call
    assert "effort" not in call["output_config"]


async def test_anthropic_repairs_once_then_fails() -> None:
    client = fake_anthropic_client(message("not json"), message(json.dumps(GOOD)))
    assert (await AnthropicJudge(client=client).judge(canonical_text="c", task_text="t")).reason
    assert "rejected" in client.messages.calls[1]["messages"][0]["content"]

    always_bad = fake_anthropic_client(message('{"verdict": "YES"}'))
    with pytest.raises(DependencyUnavailable, match="invalid output after one retry"):
        await AnthropicJudge(client=always_bad).judge(canonical_text="c", task_text="t")
    assert len(always_bad.messages.calls) == 2


@pytest.mark.parametrize(
    ("reply", "match"),
    [
        (message("", stop_reason="refusal", category="cyber"), "refusal, cyber"),
        (RuntimeError("connection reset"), "anthropic RuntimeError"),
    ],
)
async def test_anthropic_failures_are_dependency_errors(reply: Any, match: str) -> None:
    with pytest.raises(DependencyUnavailable, match=match):
        await AnthropicJudge(client=fake_anthropic_client(reply)).judge(
            canonical_text="c", task_text="t"
        )


async def test_anthropic_truncated_output_goes_through_the_retry() -> None:
    client = fake_anthropic_client(message("", stop_reason="max_tokens"), message(json.dumps(GOOD)))
    assert (await AnthropicJudge(client=client).judge(canonical_text="c", task_text="t")).verdict


async def test_anthropic_extraction_is_free_text() -> None:
    client = fake_anthropic_client(message('{"constraints": []}'))
    raw = await AnthropicJudge(client=client).extract_constraints(
        instruction="Refund up to $5", issued_at=datetime(2026, 11, 7, tzinfo=UTC)
    )
    assert raw == '{"constraints": []}'
    assert "format" not in client.messages.calls[0]["output_config"]


def test_judge_needs_a_model() -> None:
    with pytest.raises(ValueError, match="model"):
        OpenAICompatJudge(base_url="http://x", model="", transport=openai_transport("{}"))
    with pytest.raises(ValueError, match="BASE_URL"):
        OpenAICompatJudge(base_url="", model="m", transport=openai_transport("{}"))


# --------------------------------------------------------------------------- openai_compat


async def test_openai_compat_request_and_errors() -> None:
    seen: list[dict[str, Any]] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(GOOD)}}]})

    judge = OpenAICompatJudge(base_url="http://llm.test/v1/", model="m", api_key="k",
                              transport=httpx.MockTransport(ok))  # fmt: skip
    assert (await judge.judge(canonical_text="c", task_text="t")).verdict == "ENTAILED"
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert seen[0]["messages"][0]["role"] == "system"
    await judge.aclose()

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    broken = OpenAICompatJudge(base_url="http://x", model="m", transport=httpx.MockTransport(down))
    with pytest.raises(DependencyUnavailable, match="openai_compat HTTPStatusError"):
        await broken.judge(canonical_text="c", task_text="t")


# --------------------------------------------------------------------------- caches


async def _sqlite_cache() -> JudgeCache:
    return (await SQLiteEventStore("sqlite:///:memory:").initialize()).judge_cache()


async def _memory_cache() -> JudgeCache:
    return InMemoryJudgeCache()


@pytest.mark.parametrize("factory", [_memory_cache, _sqlite_cache], ids=["memory", "sqlite"])
async def test_judge_cache_contract(factory: Any) -> None:
    cache = await factory()
    assert isinstance(cache, JudgeCache)
    assert await cache.get("k") is None
    first = JudgeResult(verdict="ENTAILED", reason="r", confidence=0.123456789, evidence="e")
    await cache.put("k", first, model="m", prompt_version="v1")
    await cache.put("k", first.model_copy(update={"verdict": "AMBIGUOUS"}), model="m",
                    prompt_version="v1")  # fmt: skip
    assert await cache.get("k") == first  # first answer wins, value round-trips exactly
    none_ev = JudgeResult(verdict="CONTRADICTED", reason="r", confidence=1.0)
    await cache.put("k2", none_ev, model="m", prompt_version="v1")
    assert await cache.get("k2") == none_ev


async def test_sqlite_cache_survives_projection_rebuild() -> None:
    store = await SQLiteEventStore("sqlite:///:memory:").initialize()
    cache = store.judge_cache()
    await cache.put("k", JudgeResult(verdict="ENTAILED", reason="r", confidence=1.0),
                    model="m", prompt_version="v1")  # fmt: skip
    await store.project_all()
    assert await cache.get("k") is not None
    await store.close()


# --------------------------------------------------------------------------- local models


class FakeCrossEncoder:
    def __init__(self, labels: dict[int, str], probs: list[float]) -> None:
        self.model = SimpleNamespace(config=SimpleNamespace(id2label=labels))
        self.probs = probs
        self.calls: list[Any] = []

    def predict(self, pairs: Any, apply_softmax: bool = False) -> list[list[float]]:
        assert apply_softmax
        self.calls.append(pairs)
        return [self.probs]


def test_cross_encoder_nli_reads_the_entailment_label() -> None:
    model = FakeCrossEncoder({0: "contradiction", 1: "entailment", 2: "neutral"}, [0.1, 0.7, 0.2])
    nli = CrossEncoderNLI("fake", model=model)
    assert isinstance(nli, NLIModel)
    assert nli.entailment("premise", "hypothesis") == pytest.approx(0.7)
    assert model.calls == [[("premise", "hypothesis")]]
    other_order = FakeCrossEncoder({0: "ENTAILMENT", 1: "neutral", 2: "contradiction"}, [0.9, 0, 0])
    assert CrossEncoderNLI("fake", model=other_order).entailment("p", "h") == pytest.approx(0.9)
    with pytest.raises(ValueError, match="entailment label"):
        CrossEncoderNLI("fake", model=FakeCrossEncoder({0: "yes", 1: "no"}, [1, 0]))


def test_sentence_transformer_embedder() -> None:
    class FakeST:
        def get_sentence_embedding_dimension(self) -> int:
            return 2

        def encode(self, texts: list[str], normalize_embeddings: bool = False) -> list[list[float]]:
            assert normalize_embeddings
            return [[0.6, 0.8] for _ in texts]

    emb = SentenceTransformerEmbedder("fake", model=FakeST())
    assert isinstance(emb, Embedder)
    assert emb.dimension == 2
    assert emb.encode(["a", "b"]) == [[0.6, 0.8], [0.6, 0.8]]


async def test_anthropic_request_through_the_real_sdk_offline() -> None:
    """The real SDK serialises our request as intended (mock transport: no network)."""
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    seen: dict[str, Any] = {}

    def handler(request: Any) -> Any:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(GOOD)}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
        })  # fmt: skip

    client = anthropic.AsyncAnthropic(
        api_key="test",
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    result = await AnthropicJudge(client=client).judge(canonical_text="c", task_text="up to $100")
    assert result.verdict == "ENTAILED"
    assert seen["headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01"
    body = seen["body"]
    assert (body["model"], body["fallbacks"]) == ("claude-opus-5-5", "default")
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
