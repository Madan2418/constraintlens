# ADR 0001: Record architecture decisions

- **Status:** accepted
- **Date:** 2026-11-07
- **Spec reference:** ARCHITECTURE.md §17 (`docs/decisions/`)

## Context

`ARCHITECTURE.md` is the baseline design. Implementation will hit points where the
spec is abbreviated ("shown here in abbreviated form", §8) or silent. Those choices
must be visible and reviewable, not buried in code.

## Decision

Every decision that fills a gap in, or deviates from, `ARCHITECTURE.md` gets an ADR in
`docs/decisions/`, numbered sequentially and using `0000-adr-template.md`. When a
decision changes the spec itself, `ARCHITECTURE.md` is updated in the same change.

## Consequences

Reviewers can audit every departure from the baseline in one place.
