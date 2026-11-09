# ConstraintLens developer tasks (ARCHITECTURE.md §19.2).
# Targets for later phases fail loudly instead of pretending to work.

.PHONY: setup test extraction-eval coverage-domain lint format-check format typecheck imports check integration eval bench demo demo-baseline demo-protected demo-async latency serve

setup:
	uv sync --all-extras

test:
	uv run pytest tests/unit tests/contract tests/property tests/e2e \
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
	@test -n "$(REDIS_URL)" || (echo "set REDIS_URL to a running Redis" && exit 1)
	REDIS_URL=$(REDIS_URL) uv run pytest -m integration tests

eval:
	@echo "Evaluation harness arrives in Phase 8." && exit 1

bench:
	@echo "driftchain-bench generator arrives in Phase 6." && exit 1

# Phase 2: the unprotected "before" half of the demo (no Redis server needed).
demo-baseline:
	uv run python -m pipelines.baseline.refund_langgraph --mutation SUMMARIZATION_LOSS --hop 2
	uv run python -m pipelines.baseline.procurement_redis_workers --crash-before-ack

# Phase 3: the same drift, stopped at the tool gate (in-process service).
demo-protected:
	uv run python -m pipelines.refund_langgraph --mutation SUMMARIZATION_LOSS --hop 2 --compare
	uv run python -m pipelines.refund_langgraph --matrix --org-policy

# Phase 5 exit criterion: per-tier latency against the §10.3 budgets.
latency:
	uv run python -m eval.latency_budget --out eval/reports

# Phase 4: redelivery handled once, and a stale constraint failing closed.
demo-async:
	uv run python -m pipelines.procurement_redis_workers --crash-before-ack --dwell-minutes 2 --compare
	uv run python -m pipelines.procurement_redis_workers

serve:
	uv run python -m constraintlens.api

demo:
	@echo "Full demo stack arrives in Phase 9." && exit 1
