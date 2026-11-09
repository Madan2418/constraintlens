"""driftchain-bench: labeled asynchronous delegation chains with known drift.

ARCHITECTURE.md §13. The package is separately releasable: it imports nothing from
``constraintlens``, ``pipelines`` or ``eval`` (an import-linter contract enforces this),
so the dataset and its generator can be cited and used without the tool.
"""

GENERATOR_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"
