"""driftchain-bench command line (mirrors ``constraintlens bench ...`` of ARCHITECTURE.md §16).

python -m driftchain_bench generate --version 0.1.0 --seed 7 --size 4000
python -m driftchain_bench check
python -m driftchain_bench validate --sample 300
python -m driftchain_bench agreement --labels driftchain_bench/validation/v0.1.0/labels.csv
python -m driftchain_bench paraphrase --max-calls 200          # optional, billed
python -m driftchain_bench schema [--write]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from driftchain_bench import GENERATOR_VERSION, SCHEMA_VERSION
from driftchain_bench.generator import digest, generate, read_jsonl, statistics, write_jsonl
from driftchain_bench.release import (
    RELEASE_SEED,
    RELEASE_SIZE,
    SCHEMA_PATH,
    check_schema,
    data_path,
    manifest_path,
    read_manifest,
    schema_text,
    write_manifest,
)


def _generate(args: argparse.Namespace) -> int:
    chains = generate(version=args.version, seed=args.seed, size=args.size)
    out = Path(args.out) if args.out else data_path(args.version, args.seed, args.size)
    sha = write_jsonl(chains, out)
    manifest = {
        "name": "driftchain-bench",
        "generator_version": args.version,
        "schema_version": SCHEMA_VERSION,
        "seed": args.seed,
        "size": args.size,
        "file": out.name,
        "sha256": sha,
        "llm_paraphrase_model": None,
        "statistics": statistics(chains),
    }
    mpath = manifest_path(args.version, args.seed, args.size)
    if args.write_manifest:
        write_manifest(mpath, manifest)
        print(f"manifest written: {mpath}")
    elif mpath.exists() and read_manifest(mpath)["sha256"] != sha:
        print(f"ERROR: output differs from the committed manifest {mpath.name}", file=sys.stderr)
        return 1
    print(f"{len(chains)} chains -> {out} (sha256 {sha})")
    return 0


def _check(args: argparse.Namespace) -> int:
    check_schema()
    mpath = Path(args.manifest) if args.manifest else manifest_path()
    manifest = read_manifest(mpath)
    regenerated = generate(
        version=manifest["generator_version"], seed=manifest["seed"], size=manifest["size"]
    )
    if digest(regenerated) != manifest["sha256"] and manifest["llm_paraphrase_model"] is None:
        print("ERROR: the generator no longer reproduces the manifest", file=sys.stderr)
        return 1
    data = Path(args.data) if args.data else mpath.parent.parent / "data" / manifest["file"]
    if data.exists():
        chains = read_jsonl(data)  # validates every record against the schema
        if digest(chains) != manifest["sha256"]:
            print(f"ERROR: {data.name} does not match the manifest", file=sys.stderr)
            return 1
        print(f"{data.name}: {len(chains)} records valid, sha256 matches")
    if statistics(regenerated) != manifest["statistics"]:
        print("ERROR: statistics differ from the manifest", file=sys.stderr)
        return 1
    print(f"schema locked ({SCHEMA_PATH.name}); generator reproduces {mpath.name}")
    return 0


def _load(args: argparse.Namespace) -> tuple[list, str]:  # type: ignore[type-arg]
    path = Path(args.data) if args.data else data_path()
    if not path.exists():
        chains = generate(seed=RELEASE_SEED, size=RELEASE_SIZE)
        write_jsonl(chains, path)
    chains = read_jsonl(path)
    return chains, digest(chains)


def _validate(args: argparse.Namespace) -> int:
    from driftchain_bench.validate import write_sample

    chains, sha = _load(args)
    out = write_sample(chains, version=GENERATOR_VERSION, data_sha256=sha, n=args.sample,
                       seed=args.seed)  # fmt: skip
    print(f"blind sample of {args.sample} chains -> {out}")
    return 0


def _agreement(args: argparse.Namespace) -> int:
    from driftchain_bench.validate import agreement, read_labels, render_agreement, sample_dir

    chains, sha = _load(args)
    directory = sample_dir(GENERATOR_VERSION)
    key = json.loads((directory / "key.json").read_text(encoding="utf-8"))
    if key["data_sha256"] != sha:
        print("ERROR: the sample was drawn from a different dataset file", file=sys.stderr)
        return 1
    labels = read_labels(Path(args.labels))
    second = read_labels(Path(args.second)) if args.second else None
    report = agreement({c.chain_id: c for c in chains}, key, labels, second)
    (directory / "agreement.json").write_text(json.dumps(report, indent=2) + "\n",
                                              encoding="utf-8", newline="\n")  # fmt: skip
    md = render_agreement(report, version=GENERATOR_VERSION)
    (directory / "agreement.md").write_text(md, encoding="utf-8", newline="\n")
    print(md)
    return 0


def _paraphrase(args: argparse.Namespace) -> int:
    from driftchain_bench.paraphrase import (
        AnthropicParaphraser,
        append_cache,
        paraphrase_chains,
        read_cache,
    )

    chains, _ = _load(args)
    paraphraser = AnthropicParaphraser(model=args.model)
    cache_path = Path(args.cache)
    cache = read_cache(cache_path)
    new_chains, report, rows = asyncio.run(
        paraphrase_chains(chains, paraphraser, cache=cache, max_calls=args.max_calls,
                          concurrency=args.concurrency)
    )  # fmt: skip
    append_cache(cache_path, rows)
    out = Path(args.out) if args.out else data_path().with_suffix(".paraphrased.jsonl")
    sha = write_jsonl(new_chains, out)
    print(json.dumps({**report.as_dict(), "out": str(out), "sha256": sha}, indent=2))
    return 0


def _schema(args: argparse.Namespace) -> int:
    if args.write:
        SCHEMA_PATH.write_text(schema_text(), encoding="utf-8", newline="\n")
        print(f"wrote {SCHEMA_PATH}")
        return 0
    check_schema()
    print("schema.json matches record.py")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="driftchain_bench",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="generate the dataset (deterministic)")
    g.add_argument("--version", default=GENERATOR_VERSION)
    g.add_argument("--seed", type=int, default=RELEASE_SEED)
    g.add_argument("--size", type=int, default=RELEASE_SIZE)
    g.add_argument("--out")
    g.add_argument("--write-manifest", action="store_true",
                   help="(re)write the committed manifest; only for a new release")  # fmt: skip
    g.set_defaults(func=_generate)

    c = sub.add_parser("check", help="schema lock + generator reproduces the manifest")
    c.add_argument("--manifest")
    c.add_argument("--data")
    c.set_defaults(func=_check)

    v = sub.add_parser("validate", help="write the blind hand-validation sample")
    v.add_argument("--sample", type=int, default=300)
    v.add_argument("--seed", type=int, default=13)
    v.add_argument("--data")
    v.set_defaults(func=_validate)

    a = sub.add_parser("agreement", help="hand labels vs programmatic labels")
    a.add_argument("--labels", required=True)
    a.add_argument("--second", help="a second annotator's labels (inter-annotator kappa)")
    a.add_argument("--data")
    a.set_defaults(func=_agreement)

    p = sub.add_parser("paraphrase", help="optional LLM paraphrase batch (billed per call)")
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--max-calls", type=int, required=True)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--cache", default=str(Path(__file__).parent / "paraphrases" / "cache_v1.jsonl"))
    p.add_argument("--data")
    p.add_argument("--out")
    p.set_defaults(func=_paraphrase)

    s = sub.add_parser("schema", help="check (or --write) schema.json")
    s.add_argument("--write", action="store_true")
    s.set_defaults(func=_schema)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
