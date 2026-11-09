"""LLM paraphrase batch (ARCHITECTURE.md §13.1, §14.5, §19.4).

Template rendering already varies wording; this optional, one-time batch job rewrites hop
texts with an LLM so the surface reads like real agent output. Labels never depend on
it:

* a rewrite is **accepted only if its constraint-bearing features equal the template's**
  (``reference.features``: amounts, approval cue, permission cue, quoted note, subject
  ids). Anything else, such as a dropped amount, a lost approval cue or a refusal, keeps
  the template text. A paraphrase therefore cannot change a label;
* outputs are **stored** in a cache file keyed by ``sha256(model | prompt version | text)``
  and committed with the release, so evaluation never regenerates text and the
  paraphrased dataset is reproducible offline;
* a ``max_calls`` guard bounds spend. Each call is billed by the provider.

The paraphrase model should differ from the judge model (§14.5 "judge and generator
share a model family"). The default here is ``claude-sonnet-5-5``, while the default
judge is ``claude-opus-5-5``. Both are Claude models, which the dataset card lists as a
remaining threat.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from driftchain_bench.reference import features
from driftchain_bench.scenarios import load_scenarios

if TYPE_CHECKING:
    from collections.abc import Sequence

    from driftchain_bench.record import ChainRec

PROMPT_DIR = Path(__file__).parent / "prompts"
PROMPT_VERSION = "v1"
DEFAULT_MODEL = "claude-sonnet-5-5"


class ParaphraseError(RuntimeError):
    """The paraphraser could not produce a rewrite (API error, refusal, truncation)."""


class Paraphraser(Protocol):
    model_id: str  # "provider:model", recorded in provenance and hop surfaces

    async def paraphrase(self, text: str) -> str: ...


def load_prompt(version: str = PROMPT_VERSION) -> str:
    return (PROMPT_DIR / f"paraphrase_{version}.md").read_text(encoding="utf-8").strip()


def cache_key(model_id: str, prompt_version: str, text: str) -> str:
    return hashlib.sha256(f"{model_id}|{prompt_version}|{text}".encode()).hexdigest()


class AnthropicParaphraser:
    """Claude via the official ``anthropic`` SDK (optional extra ``judge``)."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        prompt_version: str = PROMPT_VERSION,
        timeout_s: float = 30.0,
        max_tokens: int = 1024,
        client: Any = None,
    ) -> None:
        if client is None:
            import anthropic  # lazy: optional dependency

            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=2)
        self._client = client
        self._model = model
        self._system = load_prompt(prompt_version)
        self._max_tokens = max_tokens
        self.prompt_version = prompt_version
        self.model_id = f"anthropic:{model}"

    async def paraphrase(self, text: str) -> str:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[
                    {"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": f"<task_text>\n{text}\n</task_text>"}],
                output_config={"effort": "low"},
            )
        except Exception as exc:  # anthropic.APIError and subclasses, timeouts, network
            raise ParaphraseError(f"anthropic {exc.__class__.__name__}: {exc}"[:300]) from exc
        if response.stop_reason == "refusal":
            raise ParaphraseError("model declined (refusal)")
        if response.stop_reason == "max_tokens":
            raise ParaphraseError("output truncated (max_tokens)")
        out = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        return _clean(out)


def _clean(text: str) -> str:
    out = text.strip()
    for tag in ("<task_text>", "</task_text>"):
        out = out.replace(tag, "")
    out = out.strip()
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'":
        out = out[1:-1].strip()
    return " ".join(out.split())


# --------------------------------------------------------------------------- cache


def read_cache(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["key"]] = row["output"]
    return out


def append_cache(path: Path, rows: Sequence[dict[str, str]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- batch


@dataclass
class ParaphraseReport:
    model_id: str
    hops: int = 0
    calls: int = 0
    cache_hits: int = 0
    accepted: int = 0
    rejected: int = 0  # rewrite changed a constraint-bearing feature: template kept
    failed: int = 0  # API error, refusal, truncation: template kept
    over_budget: int = 0  # max_calls reached: template kept
    examples_rejected: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {k: v for k, v in self.__dict__.items()}


async def paraphrase_chains(
    chains: Sequence[ChainRec],
    paraphraser: Paraphraser,
    *,
    cache: dict[str, str],
    prompt_version: str = PROMPT_VERSION,
    max_calls: int,
    concurrency: int = 4,
) -> tuple[list[ChainRec], ParaphraseReport, list[dict[str, str]]]:
    """Rewrite every hop text; return new chains, a report, and new cache rows."""
    report = ParaphraseReport(model_id=paraphraser.model_id)
    new_rows: list[dict[str, str]] = []
    sem = asyncio.Semaphore(concurrency)
    budget = {"left": max_calls}

    async def rewrite(text: str) -> str | None:
        key = cache_key(paraphraser.model_id, prompt_version, text)
        if key in cache:
            report.cache_hits += 1
            return cache[key]
        if budget["left"] <= 0:
            report.over_budget += 1
            return None
        budget["left"] -= 1
        async with sem:
            report.calls += 1
            try:
                out = await paraphraser.paraphrase(text)
            except ParaphraseError:
                report.failed += 1
                return None
        cache[key] = out
        new_rows.append({
            "key": key, "model": paraphraser.model_id, "prompt_version": prompt_version,
            "input": text, "output": out,
        })  # fmt: skip
        return out

    scenarios = load_scenarios()
    out_chains: list[ChainRec] = []
    for chain in chains:
        scn = scenarios[chain.scenario]
        ids = [item.split(":", 1)[1] for item in _subject_items(chain)]
        ids += [
            str(h.action.args[chain.tools[h.action.tool].subject_arg])
            for h in chain.hops
            if h.action is not None
        ]
        texts = [h.text for h in chain.hops]
        rewrites = await asyncio.gather(*(rewrite(t) for t in texts))
        hops = []
        for hop, candidate in zip(chain.hops, rewrites, strict=True):
            report.hops += 1
            if candidate is None or not candidate or candidate == hop.text:
                hops.append(hop)
                continue
            before = features(hop.text, unit=scn.unit, source=scn.words["source"], ids=ids)
            after = features(candidate, unit=scn.unit, source=scn.words["source"], ids=ids)
            if before != after:
                report.rejected += 1
                if len(report.examples_rejected) < 20:
                    report.examples_rejected.append({
                        "chain_id": chain.chain_id, "hop": str(hop.hop),
                        "template": hop.text, "paraphrase": candidate,
                        "template_features": before.describe(),
                        "paraphrase_features": after.describe(),
                    })  # fmt: skip
                hops.append(hop)
                continue
            report.accepted += 1
            surface = f"llm:{paraphraser.model_id}"
            hops.append(hop.model_copy(update={"text": candidate, "surface": surface}))
        provenance = chain.provenance.model_copy(
            update={"llm_paraphrase_model": paraphraser.model_id}
        )
        out_chains.append(
            type(chain).model_validate(
                {**chain.model_dump(), "hops": [h.model_dump() for h in hops],
                 "provenance": provenance.model_dump()}
            )
        )  # fmt: skip
    return out_chains, report, new_rows


def _subject_items(chain: ChainRec) -> list[str]:
    subject = chain.constraints[0].spec.subject
    if subject == "*":
        return []
    body = subject[1:-1] if subject.startswith("{") else subject
    return [s for s in body.split(",") if ":" in s]
