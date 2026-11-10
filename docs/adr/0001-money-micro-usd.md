# 1. Money is integer micro-USD

Status: accepted

## Context

A single LLM call costs a small fraction of a cent, and floats accumulate rounding error across thousands of calls. PayPal captures are in whole cents.

## Decision

All money is an integer count of micro-USD (1 USD = 1,000,000) in code, the database and the API. No floats anywhere in money code. Settlement rounds each task's spend up to the next cent and records the difference as a `rounding` ledger line.

## Consequences

Sums are exact and invariants can be asserted with equality. Captured is always at least spent, and the gap is visible. Prices are configured as integer micro-USD per million tokens.
