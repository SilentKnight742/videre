# SPEC A2 - Agent core (planner, executor, tools, provider)

Status: agreed design, v1. Overrides PLAN-backend.md wherever they disagree (unit model, schema, LLM-generated plans).
Builds on A1 as merged (event outbox, idempotent reducer, SSE). Read A1's code before changing it.
Principle: the agent does not need to be smart; it must be **observable, bounded, and never hang or crash**.

## 1. Unit model (ADR 0005 - write it)
| Unit | Definition | Money role (phase B) |
|---|---|---|
| **Step** (atom) | One operation with one request and one response: a model call (`step_type=llm`) or a tool call (`step_type=tool`). Cannot be paused midway; worst-case cost known before it starts, exact cost after. | Enforcement: reserve before, commit after |
| **Task** (composite, recursive) | A goal with a completion criterion. Contains child tasks or steps, never both. A task with child tasks is *composite*; a task with steps is a *leaf*. Max depth 3 (root = depth 0). | Control: holds an allocation, is what the user funds |
| **Run** (top) | One prompt, one root task, one wallet. | Settlement |

Relationships:
- **Containment**: `parent_id` tree. Cost rolls up (A1 already does this).
- **Dependency**: DAG edges **between sibling tasks only** (same `parent_id`). A cross-branch need is expressed on the ancestors that are siblings. A dependent task starts only when all its dependencies are `done`; their outputs are passed in as context.
- **Steps have no edges.** They are ordered by `turn` (0, 1, 2 ...). Steps with the same `turn` (parallel tool calls requested by one model call) ran concurrently. A "turn" is a grouping attribute, not a node.
- **Thoughts are not nodes.** They are optional content on an llm step (`thought`), often missing for external agents; no view may depend on them.
- **Actor is an attribute, not a level**: `actor` = who executed the node (`planner`, `executor`, later a subagent name).
- Abandoned work is `cancelled` (with a reason), never deleted.

## 2. Schema v2 (storage/schema.sql)
Dev database only, no migration code: set `PRAGMA user_version = 2`; on start, if the DB has another non-zero version, refuse to start with "delete videre.db (schema changed)". Note this in ADR 0005.
- `runs`: rename `scenario` -> `prompt TEXT NOT NULL`; add `title TEXT`, `provider TEXT`, `model TEXT`, `error TEXT`; status CHECK adds `completed`, `failed`, `cancelled` (keep phase B statuses).
- `nodes`: `kind` CHECK becomes (`task`, `step`); add `step_type TEXT CHECK (step_type IN ('llm','tool'))` (NULL for tasks), `actor TEXT`, `turn INTEGER`, `depth INTEGER NOT NULL`, `objective TEXT`, `thought_preview TEXT`, `output_preview TEXT`, `error TEXT`. Keep all money columns.
- Path prefixes: task `t`, step `s` -> `/t1/t2/s3`.
- `node_io`: add `thought TEXT`, `tool_name TEXT`, `tool_args_json TEXT`, `tool_result TEXT`, `cached INTEGER NOT NULL DEFAULT 0`, `error TEXT`. For a task node, `response` holds its final output.
- New `artifacts(run_id, path, bytes, sha256, node_id, created_at, PRIMARY KEY(run_id, path))`.
- `domain/states.py`: replace `SUBTASK`/`CALL` with `STEP`; add `StepType`; add an explicit allowed-transition table and make `RunService.set_node_status` reject illegal transitions (`ValueError`). Transitions: blocked->ready|cancelled; waiting_funds->ready|cancelled; ready->running|cancelled; running->done|failed|paused_budget|cancelled; paused_budget->running|cancelled. Terminal: done, failed, cancelled.
- `add_node` validates: depth <= 3; dependencies are siblings (same parent) and tasks; steps only under leaf tasks; a task gets either child tasks or steps, not both.

## 3. Events (domain/events.py) - generic, provider-neutral
Extend, do not rename, A1's vocabulary. Payloads carry absolute values (reducer stays idempotent).
| Type | Payload (new fields) |
|---|---|
| `run.created` | `prompt, title, provider, model, source="native"` |
| `run.status` | `status, error?` |
| `node.created` | existing + `step_type, actor, turn, depth, objective` |
| `node.status` | `status, reason?` |
| `node.thought` | `text` (<= 500 chars; full text in node_io) |
| `node.io` | `model?, tokens_in, tokens_out, latency_ms, cached, tool_name?, preview` (<= 280 chars) |
| `node.output` | `preview` (<= 280 chars) |
| `node.cost` | unchanged |
| `artifact.written` | `path, bytes, node_id` |
`NodeView` gains `step_type, actor, turn, depth, thought_preview, output_preview, error, tokens_in, tokens_out`. Heavy text never goes into events or snapshots; `GET /api/nodes/{id}` serves it. The frontend's live log = replaying the stream from seq 0.

## 4. Provider interface (agent/llm/) - ADR 0006
```python
class LLMRequest(BaseModel):
    system: str
    messages: list[Message]            # role: user|model, text
    response_schema: dict | None       # JSON schema; provider must return JSON matching it
    max_output_tokens: int
    temperature: float = 0.2

class LLMResponse(BaseModel):
    text: str
    parsed: dict | None
    tokens_in: int
    tokens_out: int                    # include any thinking tokens the provider bills as output
    latency_ms: int
    model: str
    finish_reason: str
    cached: bool = False
    thought_summary: str | None = None # only if the provider exposes one

class LLMProvider(Protocol):
    name: str
    model: str
    async def generate(self, req: LLMRequest) -> LLMResponse: ...
```
Implementations and wrappers (compose them, do not merge them):
- `GeminiProvider` (google-genai, `client.aio.models.generate_content`, JSON mime type + response schema, system instruction, max output tokens). Read tokens from `usage_metadata` (prompt, candidates, and thoughts token counts if present). If the SDK and model support thought summaries, fill `thought_summary`; verify, do not assume.
- `FakeProvider`: scripted responses keyed by a matcher; counts calls. **All non-live tests use it. No test touches the network.**
- `CachingProvider(inner, store, replay: bool)`: key = sha256(model + canonical JSON of the request). Normal mode: read-through, write on miss. A repeated identical request is served from cache even with DEMO_REPLAY=0; add `LLM_CACHE=readthrough|off|replay` in A2e so a demo can be forced live. `DEMO_REPLAY=1`: cache only; a miss raises `ReplayMiss` (never silently calls the network).
- `RateLimitedProvider(inner, rpm)`: async token bucket (`GEMINI_RPM`, default 10); on 429 / resource-exhausted, retry with exponential backoff + jitter, honouring a server retry delay if given; max 3 attempts; then raise `ProviderError`.
- Cost (domain, pure): `cost_micro = ceil((tokens_in * PRICE_IN_PER_MTOK + tokens_out * PRICE_OUT_PER_MTOK) / 1_000_000)` in integer arithmetic. Cached responses are still metered (it is list-price metering) but flagged `cached`.
- Worst-case estimate (for phase B's reserve): `estimate_max_micro(req)` = estimated input tokens (chars / 4, rounded up, + 10% margin) at input price + `max_output_tokens` at output price.

## 5. Planner (agent/planner.py)
- The run's root task (depth 0, title = shortened prompt, actor `planner`) is created at run creation. Planning is an llm step under the root (`turn 0`, title "Plan the work").
- Output schema:
```json
{"title": "short run title",
 "tasks": [{"key": "t1", "title": "<= 60 chars", "objective": "one sentence, verifiable",
            "depends_on": ["sibling keys"],
            "subtasks": [{"key": "t1a", "title": "...", "objective": "...", "depends_on": ["sibling keys"]}]}]}
```
- Bounds: 3-7 top-level tasks; each 0-4 subtasks (a task with subtasks is composite; one without is a leaf). Planner never goes deeper than depth 2; depth 3 is reserved for later dynamic expansion.
- The system prompt tells the model: only the tools in the registry exist (list them with descriptions); there is no internet; the last top-level task must assemble the deliverable (for a website: write `index.html`, inline CSS, no external assets); keep tasks concrete.
- Validation (pure function, unit-tested): unique keys; `depends_on` references siblings only; acyclic (Kahn); bounds; non-empty titles/objectives; trims over-long titles. On failure: **one repair attempt** = a second llm step "Repair plan" with the validator's errors; second failure -> run `failed` with the errors in `runs.error` and a `run.status` event. No silent fallback to a scripted plan.
- On success: create nodes in topological order through `RunService` (so every node emits `node.created`), set initial statuses (`ready` if no deps, else `blocked`).

## 6. Executor (services/executor.py + agent/loop.py)
Scheduling:
- Root becomes `running` after planning. A composite task becomes `running` when it is ready, then its children are scheduled; it becomes `done` when all children are `done`; `failed` once every child is terminal and at least one is `failed` or `cancelled`.
- When a task fails, every task that depends on it (transitively, among siblings) becomes `cancelled` with reason `dependency failed: <title>`. Other branches continue.
- When a task becomes `done`, re-check its siblings: `blocked` -> `ready` when all deps are done.
- Only leaf tasks consume concurrency: global `asyncio.Semaphore(MAX_PARALLEL_TASKS)` (default 3).
- Run ends: root `done` -> run `completed`; root `failed` -> run `failed`. Close nothing financial in phase A.
- One `asyncio.Task` per run, kept in a registry on `app.state`; `cancel(run_id)` cancels it; every running node becomes `cancelled` (reason `user cancelled`), run `cancelled`. Shutdown cancels all runs cleanly.

Leaf task loop (bounded ReAct):
- Context sent each turn: run prompt; ancestor objectives; this task's objective; outputs (summaries) of its dependencies and of its ancestors' dependencies; current workspace file list; previous turns of this task (thoughts, tool calls, tool results truncated to 4,000 chars each).
- Turn response schema:
```json
{"thought": "<= 300 chars, why the next action",
 "tool_calls": [{"tool": "write_file", "args": {"path": "index.html", "content": "..."}}],
 "final": {"summary": "<= 280 chars", "output": "the task's result"} }
```
  `tool_calls` max 3 per turn; `final` null until done. Either tools or final (if both: run tools, then finish).
- `MAX_TURNS` (default 4). On the last allowed turn the request removes `tool_calls` from the schema and instructs "finish now".
- Each turn: call the `before_call(node, estimate)` hook (no-op returning allow in phase A; becomes `reserve` in phase B) -> create the llm step (`running`) -> generate -> write `node_io` (prompt, response, thought, tokens, latency, cached) -> `record_cost` -> emit `node.thought`, `node.io` -> step `done`. Then create one tool step per tool call (same `turn`, actor `executor`), run them concurrently, record each result + its declared price, emit `node.io` and, for writes, `artifact.written`.
- `finish_reason` MAX_TOKENS (truncated JSON) is treated like invalid JSON; the repair step asks for a shorter answer (e.g. split the file into two `write_file` calls).
- Invalid JSON from the model: one "repair" llm step in the same turn with the parse error; if still invalid, the task `failed` (error stored on the node).
- Unknown tool or bad args: the tool step `failed` with the error; the error is fed back to the model next turn (the task does not fail for one bad tool call).
- Task `done`: store `final.output` in the task's `node_io.response`, `final.summary` as `output_preview`, emit `node.output`.

## 7. Tools (agent/tools/) - registry
Each tool: `name, description, args JSON schema, price_micro, async handler(ctx, args) -> ToolResult(text, data, artifact?)`. Mock tools are labelled `mock` in their description and in the UI.
| Tool | Behaviour | Price |
|---|---|---|
| `write_file(path, content)` | Writes inside `WORKSPACE_DIR/<run_id>/`. Reject absolute paths, `..`, symlinks, > 200 KB, > 50 files per run. Records an `artifacts` row. | 0 |
| `read_file(path)` | Same sandbox; returns up to 20 KB. | 0 |
| `list_files()` | Workspace listing. | 0 |
| `check_domain(name)` | **Mock**, deterministic (hash of name): available yes/no + a quoted yearly price. Real money only moves in phase B. | 1,000 micro ($0.001 lookup fee) |
No network tools in v1. The planner prompt says so.

## 8. API (api/)
```
POST /api/runs                     {prompt} -> 201 {run_id}        # creates run + root task; validates 1..2,000 chars
POST /api/runs/{id}/start          -> 202 ; 409 if already started
POST /api/runs/{id}/cancel         -> 202
GET  /api/runs/{id}                snapshot (A1, extended fields)
GET  /api/runs/{id}/stream         SSE (A1); ends on run completed|failed|cancelled|closed
GET  /api/nodes/{id}               node + full io (prompt, response, thought, tool args/result)
GET  /api/runs/{id}/artifacts      list
GET  /api/runs/{id}/artifacts/{path}   raw file
```
Security: artifacts are agent-written. Serve them with `Content-Security-Policy: sandbox`, `X-Content-Type-Options: nosniff`, correct content type, never from a path outside the run workspace. The frontend will show HTML in a sandboxed iframe.

## 9. Config additions (.env.example + config.py)
`GEMINI_RPM=10`, `MAX_PARALLEL_TASKS=3`, `MAX_TURNS=4`, `MAX_OUTPUT_TOKENS=8192`, `GEMINI_THINKING_BUDGET=1024` (set explicitly via `ThinkingConfig.thinking_budget`), `WORKSPACE_DIR=./runs` (git-ignored), `DEMO_REPLAY=0`. Never log prompts at INFO with secrets; never log the API key.
Why 8192: a whole `index.html` travels inside one `write_file` tool call, so 2048 truncates it, and thinking tokens may count against the output limit. A2c's live test must check that the planner's `finish_reason` is not MAX_TOKENS.

## 10. Tests (offline unless marked `live`)
Unit: plan validation (cycle, unknown dep, cross-level dep, bounds, duplicate keys); cost rounding; transition table; workspace sandbox (`..`, absolute, size); rate limiter backoff with a fake clock; cache key stability; replay miss raises.
Integration (FakeProvider):
1. Diamond plan (t1 -> t2, t3 -> t4) with one composite task: no task starts before its deps are `done`; never more than 3 leaf tasks running at once; root `rollup_spent_micro` == sum of all step costs; snapshot == replay of all events.
2. A turn with 2 parallel tool calls -> two tool steps with the same `turn`.
3. Task that never returns `final` -> forced final on the last turn.
4. Invalid JSON twice -> task failed -> its dependents `cancelled` with reason; an independent branch still runs to `done`; the composite parent counts a `failed` or `cancelled` child as failure, so the root and the run end `failed`, and artifacts from completed branches are kept.
5. Planner invalid twice -> run `failed` with errors.
6. Cancel mid-run -> all running nodes `cancelled`, run `cancelled`, stream ends.
7. Artifact path traversal attempt -> tool step failed, nothing written outside the workspace.
Live (skipped by default): one real planner call returns a valid plan.

## 11. Delivery: five small PRs, in order (one branch each)
| PR | Branch | Content | Model / effort |
|---|---|---|---|
| A2a | `feat/a2a-provider` | provider interface, Gemini, Fake, Caching, RateLimited, cost math, tests | Sonnet, medium |
| A2b | `feat/a2b-unit-model` | schema v2, states + transitions, RunService validation, events + reducer, ADR 0005/0006 | Sonnet, medium |
| A2c | `feat/a2c-planner` | planner prompt, validation, repair, node creation, tests | Fable, high |
| A2d | `feat/a2d-executor` | scheduler, leaf loop, tools, cancellation, integration tests | Fable, high |
| A2e | `feat/a2e-api-demo` | endpoints, artifact serving, `make demo PROMPT="..."` CLI that runs and prints events | Sonnet, medium |

## 12. A2 acceptance (all must pass before A3)
1. `make lint` and `make test` green with no network.
2. `make demo PROMPT="build a website for my flower shop"` against real Gemini: 3-7 tasks planned, run completes, `runs/<id>/index.html` opens in a browser and looks like a page for that business, total metered cost printed.
3. During that run, `curl -N localhost:8000/api/runs/<id>/stream` shows events live.
4. Re-run with `DEMO_REPLAY=1`: zero provider network calls (assert the inner provider's call counter) and the same sequence of event types and titles.
5. No illegal status transition is ever emitted (assert in tests via the transition table).

## 13. Not in A2
Budget engine, funding, pausing (phase B; only the `before_call` hook exists). Frontend (A3). Claude/OpenAI providers and external-agent adapters (v2; the interfaces above are what they will plug into).
