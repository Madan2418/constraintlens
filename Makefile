# ConstraintLens developer tasks (ARCHITECTURE.md §19.2).
# Targets for later phases fail loudly instead of pretending to work.

.PHONY: setup test extraction-eval coverage-domain lint format-check format typecheck imports check integration eval bench demo

setup:
	uv sync

test:
	uv run pytest tests/unit tests/contract tests/property \
		--cov=constraintlens --cov-report=term-missing

# Phase 1 exit criterion: extraction accuracy on the hand-written sets.
extraction-eval:
	uv run python -m eval.extraction_accuracy --out eval/reports
	uv run python -m eval.extraction_accuracy --gold eval/extraction/probe_v1.jsonl --out eval/reports

# 90% line coverage target on the domain core (§20).
coverage-domain:
	uv run pytest tests/unit tests/contract tests/property \
		--cov=constraintlens.domain --cov-fail-under=90 -q

lint: format-check typecheck imports

format-check:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

imports:
	uv run lint-imports

check: lint test coverage-domain

integration:
	@echo "Integration suite (Redis + SQLite via Docker) arrives in Phases 3-4." && exit 1

eval:
	@echo "Evaluation harness arrives in Phase 8." && exit 1

bench:
	@echo "driftchain-bench generator arrives in Phase 6." && exit 1

demo:
	@echo "Demo stack arrives in Phase 9." && exit 1
