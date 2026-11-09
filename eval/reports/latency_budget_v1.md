# Per-tier latency vs §10.3 budgets

Measured 2026-11-09T16:52:03+00:00 on Windows-11-10.0.26200-SP0 (Python 3.13.15), 30 iterations over 7 texts x 3 checkpoints.

| Tier | n | p50 ms | p95 ms | p99 ms | p95 budget ms | within | note |
|---|---|---|---|---|---|---|---|
| T0 | 630 | 0.047 | 0.095 | 0.156 | 5.0 | yes |  |
| T1a | 210 | 0.03 | 0.054 | 0.088 | 5.0 | yes |  |
| T1b | 330 | 0.032 | 0.042 | 0.069 | 5.0 | yes |  |
| T1c | 150 | 0.125 | 0.37 | 1.433 | 100.0 | yes | fake model: routing overhead only |
| T2 | 150 | 0.048 | 0.09 | 0.123 | 3000.0 | yes | fake model: routing overhead only |

End to end without escalation (check + decide + record to SQLite): p50 5.022 ms, p95 6.732 ms against a 150.0 ms budget: within.

Tier 1c and Tier 2 ran with deterministic fakes, so their rows measure routing only.
Real model latency is bounded by the hard timeouts (500 ms, 10 s); a timeout
escalates (1c) or degrades the verdict (2), as the test suite checks.
