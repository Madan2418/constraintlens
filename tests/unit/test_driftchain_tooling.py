"""driftchain-bench tooling: hand validation, paraphrase batch, CLI (§13.6, §19.4, ADR 0008)."""

from __future__ import annotations

import csv
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from driftchain_bench import GENERATOR_VERSION
from driftchain_bench.__main__ import main
from driftchain_bench.generator import digest, generate
from driftchain_bench.operators import DRIFT_OPERATORS
from driftchain_bench.paraphrase import (
    AnthropicParaphraser,
    ParaphraseError,
    append_cache,
    cache_key,
    load_prompt,
    paraphrase_chains,
    read_cache,
)
from driftchain_bench.release import RELEASE_SEED, RELEASE_SIZE
from driftchain_bench.validate import (
    CSV_FIELDS,
    HandLabel,
    agreement,
    blind_view,
    cohen_kappa,
    load_exclusions,
    read_labels,
    render_agreement,
    sample_dir,
    stratified_sample,
    write_sample,
)

if TYPE_CHECKING:
    from pathlib import Path

    from driftchain_bench.record import ChainRec


@pytest.fixture(scope="module")
def chains() -> list[ChainRec]:
    return generate(seed=RELEASE_SEED, size=RELEASE_SIZE)


# --------------------------------------------------------------------------- sampling


def test_stratified_sample_covers_every_cell(chains: list[ChainRec]) -> None:
    sample = stratified_sample(chains, n=300, seed=13)
    assert len(sample) == 300
    assert len({c.chain_id for c in sample}) == 300
    controls = sum(c.label.mutation_op == "NONE" for c in sample)
    assert controls == 150
    cells = {(c.scenario, c.label.mutation_op) for c in sample}
    scenarios = {c.scenario for c in chains}
    assert {(s, o) for s in scenarios for o in DRIFT_OPERATORS} <= cells
    assert {c.scenario for c in sample if c.label.mutation_op == "NONE"} == scenarios
    assert any(c.label.hard_negative for c in sample)
    assert stratified_sample(chains, n=300, seed=13) == sample  # seeded


def test_blind_view_hides_every_label(chains: list[ChainRec]) -> None:
    for c in chains[:200]:
        view = blind_view("v001", c)
        text = json.dumps(view)
        for leak in ("mutation_op", "mutated_hop", "violated", "hard_negative", "split",
                     "provenance", "chain_id", "variant", c.chain_id, "surface"):  # fmt: skip
            assert leak not in text
        assert len(view["hops"]) == len(c.hops)
        assert view["constraints"][0]["fresh_for_s"] == 6 * 3600


def test_committed_validation_sample_is_reproducible(
    tmp_path: Path, chains: list[ChainRec]
) -> None:
    out = write_sample(chains, version=GENERATOR_VERSION, data_sha256=digest(chains), n=300,
                       seed=13, out_dir=tmp_path)  # fmt: skip
    committed = sample_dir(GENERATOR_VERSION)
    for name in ("blind.jsonl", "sheet.md", "labels_template.csv", "key.json"):
        assert (out / name).read_bytes() == (committed / name).read_bytes(), name
    key = json.loads((committed / "key.json").read_text(encoding="utf-8"))
    assert key["size"] == 300
    assert key["data_sha256"] == digest(chains)
    assert set(key) == {"generator_version", "data_sha256", "sample_seed", "size", "samples"}
    with (committed / "labels_template.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 300
    assert tuple(rows[0]) == CSV_FIELDS
    assert all(not r["drift"] for r in rows)  # blank: nobody has labeled yet
    assert load_exclusions(GENERATOR_VERSION) == {}


# --------------------------------------------------------------------------- agreement


def test_cohen_kappa_known_values() -> None:
    assert cohen_kappa([]) is None
    assert cohen_kappa([(True, True), (False, False)]) == pytest.approx(1.0)
    assert cohen_kappa([(True, True)] * 4) is None  # chance agreement is 1
    # Classic 2x2 example: a=20, b=5, c=10, d=15 -> po=0.7, pe=0.5, kappa=0.4
    pairs = [(True, True)] * 20 + [(True, False)] * 5 + [(False, True)] * 10
    pairs += [(False, False)] * 15
    assert cohen_kappa(pairs) == pytest.approx(0.4)


def _key_and_labels(sample: list[ChainRec]) -> tuple[dict[str, Any], dict[str, HandLabel]]:
    key = {"samples": {f"v{i:03d}": c.chain_id for i, c in enumerate(sample)}}
    labels = {
        f"v{i:03d}": HandLabel(
            drift=c.label.drift,
            culprit_hop=c.label.mutated_hop,
            violation=c.label.violation_at_hop is not None,
            violation_hop=c.label.violation_at_hop,
        )
        for i, c in enumerate(sample)
    }
    return key, labels


def test_agreement_report(chains: list[ChainRec]) -> None:
    sample = stratified_sample(chains, n=60, seed=1)
    key, labels = _key_and_labels(sample)
    by_id = {c.chain_id: c for c in chains}
    perfect = agreement(by_id, key, labels, second=labels)
    assert perfect["labeled"] == 60
    assert perfect["drift"]["kappa"] == pytest.approx(1.0)
    assert perfect["violation"]["agreement"] == 1.0
    assert perfect["culprit_hop"]["accuracy"] == 1.0
    assert perfect["disagreements"] == []
    assert perfect["inter_annotator"]["drift_kappa"] == pytest.approx(1.0)

    sid = next(s for s, cid in key["samples"].items() if by_id[cid].label.drift)
    wrong = dict(labels)
    wrong[sid] = HandLabel(drift=False, culprit_hop=None, violation=False, violation_hop=None)
    del wrong[next(iter(k for k in key["samples"] if k != sid))]  # one row left unlabeled
    report = agreement(by_id, key, wrong)
    assert report["labeled"] == 59
    assert report["drift"]["kappa"] < 1.0
    assert [d["sample_id"] for d in report["disagreements"]] == [sid]
    md = render_agreement(report, version="0.1.0")
    assert "Cohen's kappa" in md
    assert sid in md


def test_read_labels(tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(
        "sample_id,drift,culprit_hop,violation,violation_hop,notes\n"
        "v001,yes,2,no,,\nv002,,,,,\nv003,N,,Yes,3,odd\n",
        encoding="utf-8",
    )
    labels = read_labels(path)
    assert labels == {
        "v001": HandLabel(drift=True, culprit_hop=2, violation=False, violation_hop=None),
        "v003": HandLabel(drift=False, culprit_hop=None, violation=True, violation_hop=3),
    }
    path.write_text("sample_id,drift,culprit_hop,violation,violation_hop,notes\nv1,maybe,,,,\n",
                    encoding="utf-8")  # fmt: skip
    with pytest.raises(ValueError, match="yes or no"):
        read_labels(path)


# --------------------------------------------------------------------------- paraphrase


class FakeParaphraser:
    """Rewrites by rule; ``mode`` picks a faithful, lossy, or failing rewrite."""

    model_id = "fake:para-1"

    def __init__(self, mode: str = "faithful") -> None:
        self.mode = mode
        self.calls = 0

    async def paraphrase(self, text: str) -> str:
        self.calls += 1
        if self.mode == "fail":
            raise ParaphraseError("boom")
        if self.mode == "lossy":  # drops every amount
            return " ".join(w for w in text.split() if not any(ch.isdigit() for ch in w))
        return "As requested: " + text


async def test_paraphrase_accepts_only_feature_preserving_rewrites(chains: list[ChainRec]) -> None:
    subset = chains[:12]
    out, report, rows = await paraphrase_chains(subset, FakeParaphraser(), cache={}, max_calls=1000)
    n_hops = sum(len(c.hops) for c in subset)
    assert report.calls + report.cache_hits == n_hops  # repeated texts are asked once
    assert report.accepted == n_hops
    assert len(rows) == report.calls
    for before, after in zip(subset, out, strict=True):
        assert after.label == before.label  # a paraphrase never changes a label
        assert after.instruction == before.instruction  # T0 is the user's own words
        assert after.provenance.llm_paraphrase_model == "fake:para-1"
        assert all(h.surface == "llm:fake:para-1" for h in after.hops)
        assert all(h.text.startswith("As requested: ") for h in after.hops)

    lossy, report, _ = await paraphrase_chains(subset, FakeParaphraser("lossy"), cache={},
                                               max_calls=1000)  # fmt: skip
    assert report.rejected > 0
    assert report.examples_rejected
    for before, after in zip(subset, lossy, strict=True):
        for h0, h1 in zip(before.hops, after.hops, strict=True):
            if h1.surface == "template":
                assert h1.text == h0.text

    failed, report, _ = await paraphrase_chains(subset, FakeParaphraser("fail"), cache={},
                                                max_calls=1000)  # fmt: skip
    assert report.failed == n_hops
    assert [c.hops for c in failed] == [c.hops for c in subset]


async def test_paraphrase_budget_and_cache(tmp_path: Path, chains: list[ChainRec]) -> None:
    subset = chains[:5]
    para = FakeParaphraser()
    cache: dict[str, str] = {}
    _, report, rows = await paraphrase_chains(subset, para, cache=cache, max_calls=3)
    assert report.calls == 3
    assert report.over_budget == sum(len(c.hops) for c in subset) - 3
    path = tmp_path / "cache.jsonl"
    append_cache(path, rows)
    assert read_cache(path) == {r["key"]: r["output"] for r in rows}
    assert read_cache(tmp_path / "missing.jsonl") == {}
    # Stored outputs are reused without calling the model again (§19.4).
    para2 = FakeParaphraser()
    _, report2, _ = await paraphrase_chains(subset, para2, cache=read_cache(path), max_calls=0)
    assert report2.cache_hits == 3
    assert para2.calls == 0
    assert cache_key("m", "v1", "x") != cache_key("m", "v2", "x")


class _FakeMessages:
    def __init__(self, response: Any = None, exc: Exception | None = None) -> None:
        self.response, self.exc = response, exc
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return self.response


def _response(text: str, stop: str = "end_turn") -> Any:
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


async def test_anthropic_paraphraser_request_and_failures() -> None:
    messages = _FakeMessages(_response('"Refund order #1, up to $5."'))
    para = AnthropicParaphraser(client=SimpleNamespace(messages=messages))
    assert para.model_id == "anthropic:claude-sonnet-5-5"
    assert await para.paraphrase("Refund order #1, up to $5.") == "Refund order #1, up to $5."
    req = messages.requests[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["system"][0]["text"] == load_prompt("v1")
    assert "<task_text>" in req["messages"][0]["content"]
    assert req["output_config"] == {"effort": "low"}

    bad_clients = (
        _FakeMessages(_response("", "refusal")),
        _FakeMessages(_response("x", "max_tokens")),
        _FakeMessages(exc=RuntimeError("down")),
    )
    for bad in bad_clients:
        with pytest.raises(ParaphraseError):
            await AnthropicParaphraser(client=SimpleNamespace(messages=bad)).paraphrase("t")


def test_anthropic_paraphraser_builds_a_real_sdk_client() -> None:
    pytest.importorskip("anthropic")
    para = AnthropicParaphraser(api_key="test-key")  # no request is sent
    assert para.model_id.startswith("anthropic:")


# --------------------------------------------------------------------------- CLI


def test_cli_schema_and_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["schema"]) == 0
    assert main(["check"]) == 0
    assert "generator reproduces" in capsys.readouterr().out


def test_cli_generate_small(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "small.jsonl"
    assert main(["generate", "--seed", "5", "--size", "16", "--out", str(out)]) == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 16
    assert "16 chains" in capsys.readouterr().out
