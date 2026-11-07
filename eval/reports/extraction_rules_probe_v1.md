# Extraction accuracy: rule-based extractor (llm_mode=off)

Gold set: `eval/extraction/probe_v1.jsonl` (20 instructions).

| Metric | All | Probe |
|---|---|---|
| instructions | 20 | 20 |
| instruction_exact_match | 0.85 | 0.85 |
| constraint_precision | 1.0 | 1.0 |
| constraint_recall | 0.85 | 0.85 |
| constraint_f1 | 0.9189 | 0.9189 |
| false_constraint_rate | 0.0 | 0.0 |
| field: action | 0.85 | 0.85 |
| field: resource | 0.85 | 0.85 |
| field: subject | 0.85 | 0.85 |
| field: max | 0.85 | 0.85 |
| field: min | 0.85 | 0.85 |
| field: unit | 0.85 | 0.85 |
| field: authority | 0.85 | 0.85 |
| field: expiry | 0.85 | 0.85 |

## Mismatches (3)

- **probe-02** Refund the client, keeping it under a hundred dollars.
  - gold: `[('refund', 'order', '*', '100', None, 'USD', 'none', None)]`
  - got:  `[]`
- **probe-03** Wire 3,000 euros to account 4141 and make sure the transfer doesn't exceed that amount.
  - gold: `[('transfer', 'funds', 'account:4141', '3000', None, 'EUR', 'none', None)]`
  - got:  `[]`
- **probe-20** Delete user accounts after legal signs off.
  - gold: `[('delete', 'records', '*', None, None, None, 'requires_approval_always', None)]`
  - got:  `[]`
