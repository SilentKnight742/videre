# 5. Unit model: step, task, run (schema v2)

Status: accepted. Amends ADR 0004 (path prefixes and depth).

## Context

Schema v1 had three fixed node kinds (`task`, `subtask`, `call`) chosen for a scripted scenario. Phase A now plans with an LLM, and the units must carry money in phase B. Each unit needs one clear money role: what is enforced, what is controlled, and what is settled.

## Decision

- **Step** (atom): one model call (`step_type=llm`) or one tool call (`step_type=tool`), with one request and one response. A step cannot be paused midway, its worst-case cost is known before it starts and its exact cost after. Phase B reserves before a step and commits after it.
- **Task** (recursive): a goal with a completion criterion. A task holds child tasks (*composite*) or steps (*leaf*), never both. The root task is depth 0 and tasks go at most to depth 3; a step sits one level below its task. Tasks hold allocations and are what the user funds.
- **Run**: one prompt, one root task, one wallet; it is the unit of settlement. `create_run` creates the root task (actor `planner`) in the same transaction.
- **Dependencies** are DAG edges between *sibling* tasks only (same parent). A cross-branch need is expressed on the ancestors that are siblings. Steps have no edges: they are ordered by `turn`, and steps with the same turn ran concurrently.
- Thoughts are optional content on llm steps, not nodes. `actor` (planner, executor, later a subagent) is an attribute, not a level. Abandoned work is `cancelled` with a reason, never deleted.
- **Status transitions** follow one table in `domain/states.py`, and `RunService.set_node_status` rejects anything else with `ValueError`: blocked → ready | cancelled; waiting_funds → ready | cancelled; ready → running | cancelled; running → done | failed | paused_budget | cancelled; paused_budget → running | cancelled. done, failed and cancelled are terminal. A reason on failed or cancelled is kept as the node's `error`.
- **Paths** use `t` for tasks and `s` for steps, numbered per kind among siblings: `/t1` (root), `/t1/t2`, `/t1/t2/s3`.
- **One exception, at the root**: the planner's llm steps ("Plan the work", "Repair plan") sit directly under the root, alongside its child tasks (SPEC-A2 section 5). Only steps whose actor is `planner` may do this; every other task follows the composite-or-leaf rule.
- **Schema versioning, dev only**: there is no migration code. `Store.open()` stamps `PRAGMA user_version = 2`. It refuses a database with any other version, including an unversioned v1 file that already has tables, with "delete videre.db (schema changed)".

## Consequences

- Enforcement in phase B is per step, control is per task and settlement is per run; no unit plays two roles.
- The tree is at most 5 levels deep (root task, three more task levels, then steps), so a rollup touches at most 4 ancestors.
- Events and snapshots carry only previews (thought up to 500 characters, io and output previews up to 280). Full text is in `node_io` and served per node.
- Changing the schema means deleting the dev database, which is acceptable before the first release only.
