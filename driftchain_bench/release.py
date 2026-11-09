"""Release artefacts: the locked schema, the manifest, default paths (ARCHITECTURE.md §13.6).

* ``schema.json`` is generated from ``record.ChainRec`` and committed. ``check_schema``
  fails when the model and the committed file disagree.
* A manifest pins one generated dataset: version, seed, size, the SHA-256 of the JSONL
  bytes, and its statistics. The JSONL itself is regenerable and gitignored (§17
  "gitignored if large"). The manifest is committed, and ``check`` regenerates the
  dataset and compares hashes, which proves the generator is frozen.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from driftchain_bench import GENERATOR_VERSION, SCHEMA_VERSION
from driftchain_bench.record import SCHEMA_ID, ChainRec

PACKAGE_DIR = Path(__file__).parent
SCHEMA_PATH = PACKAGE_DIR / "schema.json"
MANIFEST_DIR = PACKAGE_DIR / "manifests"
DATA_DIR = PACKAGE_DIR / "data"

RELEASE_SEED = 7
RELEASE_SIZE = 4000


def schema() -> dict[str, Any]:
    body = ChainRec.model_json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "driftchain-bench chain record",
        "description": (
            f"One labeled asynchronous delegation chain (schema {SCHEMA_VERSION}, "
            "ARCHITECTURE.md §13.5)."
        ),
        "x-schema-version": SCHEMA_VERSION,
        **body,
    }


def schema_text() -> str:
    return json.dumps(schema(), indent=2, ensure_ascii=False) + "\n"


def check_schema() -> None:
    committed = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
    if committed != schema_text():
        raise SystemExit(
            "schema.json is out of date with record.py. The schema is locked: bump "
            "SCHEMA_VERSION and run `python -m driftchain_bench schema --write` only for a "
            "deliberate format change."
        )


def data_path(version: str = GENERATOR_VERSION, seed: int = RELEASE_SEED,
              size: int = RELEASE_SIZE) -> Path:  # fmt: skip
    return DATA_DIR / f"driftchain-bench-v{version}-seed{seed}-n{size}.jsonl"


def manifest_path(version: str = GENERATOR_VERSION, seed: int = RELEASE_SEED,
                  size: int = RELEASE_SIZE) -> Path:  # fmt: skip
    return MANIFEST_DIR / f"v{version}-seed{seed}-n{size}.json"


def read_manifest(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
