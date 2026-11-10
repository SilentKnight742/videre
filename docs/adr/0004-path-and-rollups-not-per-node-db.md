# 4. Materialised path and rollup columns, not per-node databases

Status: accepted

## Context

The UI must show any subtree and its total spend instantly, without aggregating on read.

## Decision

Each node stores a `path` such as `/t3/s2/c1`, and every ancestor keeps `rollup_spent_micro` and `rollup_calls`, updated in the same transaction as the spend. Heavy text lives in `node_io` and is loaded only when a node is opened.

## Consequences

A subtree is one indexed prefix scan and totals are a single row read. Writes touch each ancestor (at most three levels). Rollups must only ever be changed alongside the spend that causes them.
