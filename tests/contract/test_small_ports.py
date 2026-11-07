"""Contracts for the small sync ports: Clock, Embedder, NLIModel, PipelineAdapter (§6.3)."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import pytest

from constraintlens.adapters.clock import FrozenClock, SystemClock
from constraintlens.adapters.models.fakes import HashEmbedder, ScriptedNLI
from constraintlens.adapters.pipeline.stub import StubAdapter
from constraintlens.domain.ports import Clock, Embedder, NLIModel, PipelineAdapter
from tests.factories import make_envelope

if TYPE_CHECKING:
    from collections.abc import Callable

# --------------------------------------------------------------------------- Clock


@pytest.fixture(params=[SystemClock, FrozenClock], ids=lambda f: f.__name__)
def clock(request: pytest.FixtureRequest) -> Clock:
    return request.param()


def test_clock_protocol_and_utc(clock: Clock) -> None:
    assert isinstance(clock, Clock)
    now = clock.now()
    assert now.tzinfo is not None
    offset = now.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


def test_clock_monotonic_never_decreases(clock: Clock) -> None:
    a = clock.monotonic()
    b = clock.monotonic()
    assert b >= a


# --------------------------------------------------------------------------- Embedder

EMBEDDERS: list[Callable[[], Embedder]] = [HashEmbedder]


@pytest.fixture(params=EMBEDDERS, ids=lambda f: f.__name__)
def embedder(request: pytest.FixtureRequest) -> Embedder:
    return request.param()


def test_embedder_shapes_and_normalisation(embedder: Embedder) -> None:
    assert isinstance(embedder, Embedder)
    texts = ["Refund up to $100.", "Refund in full.", ""]
    vecs = embedder.encode(texts)
    assert len(vecs) == len(texts)
    for v in vecs:
        assert len(v) == embedder.dimension
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-6)


def test_embedder_is_deterministic(embedder: Embedder) -> None:
    assert embedder.encode(["same text"]) == embedder.encode(["same text"])
    assert embedder.encode([]) == []


# --------------------------------------------------------------------------- NLIModel

NLIS: list[Callable[[], NLIModel]] = [ScriptedNLI]


@pytest.mark.parametrize("factory", NLIS, ids=lambda f: f.__name__)
def test_nli_returns_probability(factory: Callable[[], NLIModel]) -> None:
    nli = factory()
    assert isinstance(nli, NLIModel)
    score = nli.entailment("Refund in full.", "Refund up to 100 USD.")
    assert 0.0 <= score <= 1.0


# --------------------------------------------------------------------------- PipelineAdapter

ADAPTERS: list[tuple[Callable[[], PipelineAdapter[Any]], Callable[[], Any]]] = [
    (StubAdapter, lambda: {"task": "refund order 4471"}),
]


@pytest.mark.parametrize(("factory", "new_message"), ADAPTERS, ids=lambda f: f.__name__)
def test_pipeline_adapter_round_trip(
    factory: Callable[[], PipelineAdapter[Any]], new_message: Callable[[], Any]
) -> None:
    adapter = factory()
    assert isinstance(adapter, PipelineAdapter)
    message = new_message()
    assert adapter.read_envelope(message) is None  # no envelope: reported as stripped

    env = make_envelope()
    carried = adapter.write_envelope(message, env)
    assert adapter.read_envelope(carried) == env
    assert adapter.read_envelope(message) is None  # input not mutated
