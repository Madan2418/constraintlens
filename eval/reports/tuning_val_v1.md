# Provisional threshold tuning on `val` (Phase 8)

Configuration `R` (rules tiers), 816 `val` chains. Objective, fixed before the sweep: maximize detection recall subject to false-block rate <= 0.05 on val controls; ties: lower false-block rate, then the smaller threshold.

| dwell threshold | detection recall | false-block rate | violation prevention | precision | F1 |
|---|---|---|---|---|---|
| 300 s | 0.9828 | 0.5637 | 0.9639 | 0.5688 | 0.7206 |
| 600 s | 0.9804 | 0.5539 | 0.9639 | 0.5714 | 0.722 |
| 900 s | 0.9706 | 0.5221 | 0.9639 | 0.5798 | 0.7259 |
| 1800 s | 0.9632 | 0.2353 | 0.9639 | 0.698 | 0.8095 |
| 3600 s | 0.9632 | 0.2328 | 0.9639 | 0.6993 | 0.8103 |
| 7200 s | 0.9608 | 0.1618 | 0.9639 | 0.7355 | 0.8332 |
| 14400 s **(chosen)** | 0.9608 | 0.0858 | 0.9639 | 0.7747 | 0.8578 |
| 21600 s | 0.9608 | 0.0858 | 0.9639 | 0.7747 | 0.8578 |

Status: **provisional**. Bound met: False. Untuned (no effect without semantic scores on this host): theta_low, theta_high, delta_min.
