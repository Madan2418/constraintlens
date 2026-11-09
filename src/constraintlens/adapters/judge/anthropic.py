"""Anthropic (Claude) judge adapter (ARCHITECTURE.md §6.3, §7.4, §19.4).

Uses the official ``anthropic`` SDK (optional extra ``judge``; imported lazily so the
service runs without it). Per call:

* the versioned system prompt goes in ``system`` with ``cache_control`` (it is the same
  for every call, so repeated calls read it from the prompt cache);
* the answer is constrained with structured output (``output_config.format``, a JSON
  schema), at ``low`` effort by default: the task is a short classification;
* on models that support it, the server-side refusal fallback is enabled
  (``fallbacks="default"``), so a safety decline is retried on a fallback model in the
  same call. A refusal that still stands is a judge failure, never a verdict.

Every API, timeout or connection error becomes ``DependencyUnavailable``; Tier 2 turns
that into a degraded verdict and the fail policy decides (§10.1). The SDK retries
transient errors once (``max_retries=1``) inside the 10 s budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from constraintlens.adapters.judge.base import LLMJudge
from constraintlens.domain.errors import DependencyUnavailable

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"
#: Models that accept the server-side refusal fallback in its ``"default"`` form.
FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicJudge(LLMJudge):
    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        prompt_version: str = "v1",
        prompt_dir: str | Path | None = None,
        timeout_s: float = 10.0,
        effort: str | None = "low",
        max_tokens: int = 8000,
        fallbacks: bool = True,
        client: Any = None,
    ) -> None:
        super().__init__(model=model or DEFAULT_MODEL, prompt_version=prompt_version,
                         prompt_dir=prompt_dir)  # fmt: skip
        if client is None:
            import anthropic  # lazy: optional dependency

            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=1)
        self._client = client
        self._effort = effort or None
        self._max_tokens = max_tokens
        self._fallbacks = fallbacks and self._model in FALLBACK_MODELS

    async def _complete(self, system: str, user: str, json_schema: dict[str, Any] | None) -> str:
        output_config: dict[str, Any] = {}
        if self._effort:
            output_config["effort"] = self._effort
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
        }
        if output_config:
            request["output_config"] = output_config
        try:
            if self._fallbacks:
                response = await self._client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **request
                )
            else:
                response = await self._client.messages.create(**request)
        except Exception as exc:  # anthropic.APIError and subclasses, timeouts, network
            raise DependencyUnavailable(
                "judge", f"anthropic {exc.__class__.__name__}: {exc}"[:300]
            ) from exc
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise DependencyUnavailable("judge", f"model declined (refusal, {category})")
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        if response.stop_reason == "max_tokens" or not text:
            # Returned to the retry loop as invalid output, not as a verdict.
            return text or "{}"
        return text
