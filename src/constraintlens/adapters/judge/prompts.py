"""Versioned prompt files (ARCHITECTURE.md §7.4 "judge_prompt_v1", §17 ``docs/judge_prompts``).

A prompt file is Markdown with a ``## System`` section, a ``## User`` section, and an
optional ``## Repair hint block`` used on the single retry. Placeholders are
``{name}``; only the names passed to ``render`` are substituted, so literal JSON braces in
the examples are left alone.

The directory is ``CL_JUDGE_PROMPT_DIR`` when set, otherwise ``docs/judge_prompts`` found
by walking up from this file (a source checkout, which is how the service is run).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SECTION = re.compile(r"^## (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    system: str
    user: str
    repair: str | None = None

    def render_user(self, *, repair_hint: str | None = None, **values: str) -> str:
        text = render(self.user, **values)
        block = render(self.repair, repair_hint=repair_hint) if repair_hint and self.repair else ""
        if "{repair_hint_block}" in text:
            return text.replace("{repair_hint_block}", block)
        return f"{text}\n\n{block}".rstrip() if block else text


def render(template: str, **values: str) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def default_prompt_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "judge_prompts"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("docs/judge_prompts not found; set CL_JUDGE_PROMPT_DIR")


def load_prompt(name: str, version: str, directory: str | Path | None = None) -> PromptTemplate:
    """Load ``<name>_<version>.md`` (e.g. ``judge_v1.md``)."""
    path = Path(directory or default_prompt_dir()) / f"{name}_{version}.md"
    text = path.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    matches = list(_SECTION.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[m.group(1).strip().lower()] = text[m.end() : end].strip()
    if "system" not in sections or "user" not in sections:
        raise ValueError(f"{path.name} needs '## System' and '## User' sections")
    repair = next((v for k, v in sections.items() if k.startswith("repair hint")), None)
    return PromptTemplate(name, version, sections["system"], sections["user"], repair)
