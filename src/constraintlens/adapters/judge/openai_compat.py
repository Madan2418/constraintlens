"""OpenAI-compatible judge adapter (ARCHITECTURE.md §6.3, §18 ``CL_JUDGE_PROVIDER``).

For self-hosted or third-party models served behind a ``/chat/completions`` endpoint
(vLLM, llama.cpp server, Ollama, and similar). Plain ``httpx``; JSON mode via
``response_format={"type": "json_object"}``. The judge contract (schema validation, one
repair retry, failure → ``DependencyUnavailable``) is in ``LLMJudge``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from constraintlens.adapters.judge.base import LLMJudge
from constraintlens.domain.errors import DependencyUnavailable

if TYPE_CHECKING:
    from pathlib import Path


class OpenAICompatJudge(LLMJudge):
    provider = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        prompt_version: str = "v1",
        prompt_dir: str | Path | None = None,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(model=model, prompt_version=prompt_version, prompt_dir=prompt_dir)
        if not base_url:
            raise ValueError("CL_JUDGE_BASE_URL is required for the openai_compat provider")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_s, headers=headers,
            transport=transport,
        )  # fmt: skip

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _complete(self, system: str, user: str, json_schema: dict[str, Any] | None) -> str:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        if json_schema is not None:
            body["response_format"] = {"type": "json_object"}
        try:
            r = await self._http.post("/chat/completions", json=body)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise DependencyUnavailable(
                "judge", f"openai_compat {exc.__class__.__name__}: {exc}"[:300]
            ) from exc
        return str(content or "")
