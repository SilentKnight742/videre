# Videre - Backend Plan (v1, PayPal AI Hackathon)

Status: DRAFT for review. Deadline: Fri 13 Nov 2026 01:30 IST. Licence: Apache-2.0.
Audience: the coding session. Every milestone has acceptance criteria; do not move on until they pass.

## REVISION 1 (Tue 16:10 IST) - build the visualizer first, budgeting second
This overrides the milestone order in section 12 and the "scripted scenario / no LLM plans" decisions.
Goal of phase A: user types a free-text prompt ("build a website for my flower shop"), a real agent plans a
task tree and works through it, and the UI shows the live graph: tasks, subtasks, calls, each thought, status,
tokens and metered cost, dependencies. Budgeting logic (sections 3, 6) is phase B and is NOT built yet.

Phase A (visualizer):
- A0 scaffold, config, schema, Store, health route, pytest green (= old M0)
- A1 events + SSE + snapshot/replay (= old M2) - the live graph depends on this
- A2 agent core: (a) planner = one Gemini call with structured JSON output -> task tree with dependencies;
  validate (DAG, max ~8 tasks, max 3 levels, 2-4 subtasks each) and retry once on invalid output;
  (b) executor runs tasks respecting dependencies (Semaphore(3)); each step = a Gemini call that returns
  {thought, output}; emit node.created / node.status / node.thought / node.io / node.cost events;
  (c) a mock tool or two (domain lookup, file write) so the graph shows non-LLM calls; (d) llm_cache for
  deterministic replay of the demo (DEMO_REPLAY=1). Free-tier rate limit: global limiter + 429 retry.
- A3 frontend (separate plan): React Flow graph, live updates, node detail panel, prompt box. 
- A-checkpoint: prompt -> live graph working end to end by Wed ~18:00 IST. If not, cut scope inside the
  visualizer (fewer node kinds, no replay) - do NOT let phase B shrink to nothing.

Phase B (money), starts by Wed ~18:00 IST regardless: budget engine (old M1), funding/pause/resume,
funding_need and fund_to_unblock, PayPalRail authorize/capture/void, close. Hard stop for new features Thu ~18:00;
Thu night is demo video, README, Devpost.

Keep money hooks from day one so phase B layers on cleanly: every LLM/tool call records tokens and a metered
cost (micro-USD), nodes keep allocated/spent/reserved columns (unused in phase A), events carry cost fields,
and the runner calls a `before_call(node, est)` hook that is a no-op in phase A and becomes `reserve` in phase B.
Honest caveat: the hackathon is a PayPal hackathon. A visualizer with no PayPal is not an entry. Phase B is
the part the judges grade against the sponsor requirement; do not trade it away for polish.

## 0. One-paragraph product
A user funds an AI agent's wallet (a PayPal *authorization*), the agent plans a tree of tasks, and each task
runs only while it has money. The user sees the live task graph, clicks a stalled task to fund it
(including the unfunded upstream tasks it depends on), and at the end closes the run: spent money is
captured, the remainder is voided (returned). The single wallet cap is the default view; the graph is the
"where did the money go" view.

## 1. Scope
IN: FastAPI backend, SQLite, scripted task plan for one scenario, Gemini calls, mock vendor purchases,
PayPal sandbox rail (authorize / partial capture / void), SSE event stream, funding-need calculation, tests.
OUT (do not build): auth, multi-user, real concurrency tuning, per-node databases, deployment, LLM-generated
plans, Payouts, UPI (v2 - but keep the PaymentRail interface so UPI can drop in).

## 2. Stack and repository layout (REVISION 2: professional monorepo, set up in A0)
Python 3.11+, FastAPI, uvicorn, aiosqlite (SQLite, WAL), httpx, pydantic v2, google-genai, pytest, pytest-asyncio,
ruff (lint+format), uv (deps + lockfile). Frontend: React + TypeScript + Vite, React Flow, ESLint, Vitest.
NOTE: module paths written as `app/...` in later sections mean `backend/src/videre/...`.
```
videre/
  README.md                  # what/why, screenshot/GIF, quickstart, architecture diagram, honest limits
  LICENSE                    # Apache-2.0 (already in the GitHub repo)
  CLAUDE.md                  # instructions for the coding agent
  Makefile                   # make dev | test | lint | fmt | demo   (one command each)
  .gitignore  .editorconfig  .env.example
  .github/workflows/ci.yml   # backend: ruff + pytest ; frontend: eslint + tsc + vitest + build
  backend/
    pyproject.toml           # deps, ruff + pytest config; uv.lock committed
    src/videre/
      __init__.py
      main.py                # FastAPI app factory + router mounting
      config.py              # pydantic-settings; sandbox guard
      api/                   # routers only: runs.py, tasks.py, nodes.py, paypal.py, stream.py (thin)
      domain/                # pure logic, no I/O: models.py, states.py, budget.py (phase B)
      services/              # orchestration: runs.py, runner.py, events.py
      agent/                 # planner.py, executor.py, llm.py, tools/ (mock tools)
      rails/                 # base.py (PaymentRail), mock.py, paypal.py (phase B)
      storage/               # db.py, schema.sql, repositories.py
    tests/
      unit/  integration/  conftest.py     # `live` marker for sandbox/Gemini tests, skipped by default
  frontend/
    package.json  vite.config.ts  tsconfig.json  eslint config
    src/
      api/                   # typed client + SSE hook
      features/graph/        # React Flow canvas, node types, layout
      features/node-panel/   # thoughts, I/O, cost detail
      features/prompt/       # prompt box + run controls
      features/wallet/       # phase B
      components/  hooks/  lib/  types/
  docs/
    architecture.md          # diagram + data flow
    PLAN-backend.md  PLAN-frontend.md
    adr/                     # short decision records: 0001-money-micro-usd, 0002-sse-over-websocket,
                             # 0003-sqlite, 0004-path-and-rollups-not-per-node-db
    demo/                    # script + shot list
  scripts/
    paypal_gate_check.py     # move here from repo root with `git mv`
```
Rules: layering is api -> services -> domain/storage; `domain/` never imports I/O libraries.
Conventions: Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), one small commit per logical
step, `main` always green, no secrets in git, lockfiles committed, type hints everywhere, pydantic models at the
API boundary, API types mirrored in frontend `types/`. `.env` is git-ignored; never log secrets or full auth headers.
Refuse to start with RAIL=paypal if the base URL is not the sandbox host.
`.env.example`: PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, GEMINI_API_KEY, GEMINI_MODEL, RAIL=paypal|mock, DEMO_REPLAY=0|1.
A0 acceptance: fresh clone -> `make dev` runs backend + frontend; `make test` and `make lint` pass; CI file present.

### Git workflow (REVISION 3) - never commit directly to main
Model: trunk-based development with short-lived branches and pull requests, even though this is a solo project.
- `main` is protected by convention: always green, only changed through a merged PR. Never `git commit` or
  `git push` on main. The only exception is the very first commit that adds .gitignore/LICENSE if the repo is empty.
- One branch per milestone or job, created from an up-to-date main. Names: `feat/a0-scaffold`, `feat/a1-events-sse`,
  `feat/a2-agent-planner`, `feat/frontend-graph`, `fix/<short-bug>`, `test/<area>`, `docs/<topic>`, `chore/<task>`,
  `refactor/<area>`. Lowercase, hyphens, no personal names.
- Commits on the branch follow Conventional Commits (`feat(agent): validate planner output as a DAG`), small and
  focused: tests with the code they cover.
- Before opening a PR: `make lint` and `make test` pass locally. Then push the branch and open a PR with `gh pr create`
  (title = Conventional Commit style; body = what changed, how it was tested, the milestone's acceptance criteria
  ticked). CI must pass. Merge with squash so main has one clean commit per PR, then delete the branch and
  `git pull` main before starting the next one.
- Never force-push a shared branch, never rewrite main, never `--no-verify`. If a rebase or merge conflict is
  unclear, stop and ask.
- Phase B work branches from main as normal; features not ready for the demo stay behind config flags rather than
  long-lived branches.
- Release: when submitting, tag `v0.1.0` (`git tag -a v0.1.0 -m "PayPal AI Hackathon submission"`) and write short
  release notes. Record the commit hash that matches the demo video and Devpost text.
- Optional hardening (2 min on GitHub): Settings > Branches > protect `main`: require a PR and the CI check.

## 3. Money model
- Unit: integer micro-USD (1 USD = 1_000_000). No floats anywhere in money code.
- PayPal captures are in cents. Settlement rounds each task's total spend UP to the next cent and records the
  rounding as a ledger line (`rounding`), so captured >= spent and the difference is visible.
- Authorized total A (from PayPal) is the hard ceiling. Invariants (assert in code, test in pytest):
  1. sum(task.allocated) <= A
  2. task.spent <= task.allocated, always, by construction (see reservations)
  3. sum(captured) <= A
  4. captured + voided == A once the run is closed
- LLM cost is METERED, not charged: the free Gemini tier costs nothing, so each call is priced from token counts
  using a configurable price table (`PRICE_IN_PER_MTOK`, `PRICE_OUT_PER_MTOK`). The README and UI must say
  "metered at list price". Marketplace purchases come from a mock catalogue and are settled through the real
  PayPal capture. Never present simulated spend as real card spend.

## 4. Data model (SQLite)
```
runs(id, scenario, status[created|awaiting_approval|funded|running|closed], wallet_authorized_micro,
     paypal_order_id, paypal_auth_id, created_at, closed_at)
nodes(id, run_id, parent_id, kind[task|subtask|call], title, status, path,            -- path like "/t3/s2/c1"
      allocated_micro, spent_micro, reserved_micro, est_total_micro,
      rollup_spent_micro, rollup_calls, seq_created, started_at, finished_at)
edges(run_id, task_id, depends_on_task_id)                                            -- tasks only
node_io(node_id, model, prompt, response, tokens_in, tokens_out, latency_ms, extra_json)  -- lazy-loaded
ledger(id, run_id, ts, node_id, type[fund|unfund|reserve|release|spend|capture|void|rounding],
       amount_micro, balance_after_micro, ref)                                        -- append only
events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id, ts, type, node_id, payload_json)
settlements(id, run_id, task_id, cents, paypal_capture_id, invoice_id UNIQUE, status)
llm_cache(prompt_hash PRIMARY KEY, model, response, tokens_in, tokens_out)
```
Fast lookup without per-node databases: (a) `path` prefix queries give any subtree in one indexed range scan;
(b) `rollup_*` columns on every ancestor are updated in the SAME transaction as the spend, so the UI never
aggregates; (c) heavy text lives in `node_io` and is fetched only when a node is opened; (d) the event log lets
the client rebuild the graph from any `seq`.

Indexes: nodes(run_id, parent_id), nodes(run_id, path), events(run_id, seq), ledger(run_id, ts).

## 5. Task state machine
`blocked` (deps not done) -> `waiting_funds` (deps done, allocated == 0) -> `ready` -> `running`
-> `paused_budget` (next step cannot be reserved) -> `running` (after funding) -> `done` | `failed` | `cancelled`.
A task can hold funds while `blocked` (user pre-funds). Status is recomputed by the scheduler after every
state change and emitted as `node.status` events.

## 6. Budget engine (app/budget.py - pure functions, heavily tested)
- `fund(task, amount)`: moves micro from wallet pool to task.allocated. Pool = A - sum(allocated).
- `unfund(task, amount)`: only unspent, unreserved allocation can move back.
- `reserve(task, est)`: succeeds iff allocated - spent - reserved >= est. Reservation for an LLM call is an
  UPPER BOUND: input tokens estimate * in-rate + max_output_tokens * out-rate. Actual cost is always <= it, so
  overspend is impossible. On completion: `commit(actual)` and `release(est - actual)`.
- `funding_need(task)`: walk task + transitive dependencies once (visited set); for each unfinished node,
  `need = max(0, est_total - allocated)`; return per-node breakdown and total. Cycles are rejected at plan
  validation (topological sort).
- `fund_to_unblock(task)`: allocate exactly the breakdown from the wallet pool, all-or-nothing; if the pool
  is too small return 409 with the shortfall.
Acceptance: pytest covers each invariant, partial funding, double funding, unfund after spend, diamond
dependency counted once, pool exhaustion.

## 7. Events and live updates
- Every state change writes its event row in the same transaction (outbox), then publishes to in-process
  subscribers after commit. One writer lock around the Store.
- Types: run.created, run.funded, node.created, node.status, node.cost (spent/rollup deltas), alloc.changed,
  ledger.entry, task.paused, task.resumed, paypal.authorized, paypal.captured, paypal.voided, run.closed.
- `GET /api/runs/{id}/stream` is SSE; supports `Last-Event-ID`/`?after=seq` for resume; sends a keepalive
  comment every 15 s. The client first fetches the snapshot, then streams from the snapshot's seq.
Acceptance: a test subscribes, runs a scripted run on MockRail, and asserts the event order and that
rebuilding state from events equals the snapshot.

## 8. Runner
- asyncio scheduler, `Semaphore(3)` concurrent tasks. Loop wakes on any status change.
- Each task = list of steps (subtasks); each step = one or more calls (llm_call, vendor_purchase, mock_tool).
- Before a call: `reserve`. If it fails -> task `paused_budget`, wait on a per-task `asyncio.Event`; `fund`
  sets it. No half-charged calls.
- A failed call retries up to 2 times with backoff; failure marks the task `failed` (reserved funds released).
- Gemini free tier is rate-limited: global limiter + retry on 429. `DEMO_REPLAY=1` serves from `llm_cache` so the
  recorded video is deterministic and works offline.
Acceptance: scripted run with a wallet that is too small stalls at the right task, and completes after
`fund_to_unblock`.

## 9. Scenario (scripted plan, ~7 tasks)
"Launch a landing page for a neighbourhood bakery":
1 Research (llm) | 2 Write copy (llm, dep 1) | 3 Design brief (llm, dep 1) | 4 Build page (llm, dep 2,3)
5 Buy domain (vendor) | 6 Buy hosting/storage (vendor, dep 4) | 7 Deploy (mock tool, dep 4,5,6)
Each task has 2-4 subtasks; est_total per task is declared in the plan (tuned so a $5 wallet is not enough
for everything - the stall is the demo).

## 10. PayPal rail (app/rails)
`PaymentRail` protocol: `create_hold(amount_cents) -> (order_id, approve_url)`, `confirm_hold(order_id) ->
hold_id`, `capture(hold_id, cents, invoice_id) -> capture_id`, `release(hold_id)`. PayPalRail wraps the flow
already proven by `paypal_gate_check.py`:
- create order intent=AUTHORIZE, return_url = `http://localhost:8000/paypal/return` (our backend), cancel_url likewise
- `GET /paypal/return?token=<order_id>` -> authorize order -> store auth id -> redirect to the frontend run page
- capture with `final_capture=false`, `invoice_id = f"{run_id}-{task_id}"` (UNIQUE in `settlements`), and
  `PayPal-Request-Id` derived from the same key so retries are idempotent
- void at `POST /api/runs/{id}/close`
- settle a task when it reaches `done`: capture round-up(spent) cents; one capture per task
MockRail implements the same interface in memory (tests, offline demo). A UPI rail will implement this later.
Acceptance: a `live`-marked test (skipped by default) runs against the sandbox; all other tests use MockRail.

## 11. API
```
POST /api/runs                      {scenario, wallet_cents} -> {run_id, approve_url}
GET  /paypal/return                 (PayPal redirect) -> authorize, redirect to UI
GET  /api/runs/{id}                 snapshot: run, nodes, edges, wallet {authorized, allocated, spent, pool}
GET  /api/runs/{id}/stream          SSE
POST /api/runs/{id}/start
POST /api/tasks/{id}/fund           {amount_micro}
POST /api/tasks/{id}/unfund         {amount_micro}
GET  /api/tasks/{id}/funding-need   -> {total_micro, items:[{task_id, need_micro}]}
POST /api/tasks/{id}/fund-to-unblock
GET  /api/nodes/{id}                node + node_io
GET  /api/runs/{id}/ledger
POST /api/runs/{id}/close           capture leftovers, void remainder -> summary
```
CORS: allow only the local frontend origin.

## 12. Milestones (hours are rough; running total ~17h)
M0 scaffold, config, schema, Store, health route, pytest green (1.5h)
M1 budget.py + exhaustive tests (3h)
M2 events + SSE + snapshot/replay test (2h)
M3 runner + scripted plan + mock handlers on MockRail, CLI script that runs the scenario (4h)
M4 Gemini client, metering, cache/replay, rate limiting (1.5h)
M5 PayPalRail + approve/return flow + settlement + close (3h)
M6 API wiring, e2e script: fund small -> stall -> fund_to_unblock -> finish -> close (2h)
Checkpoint: M3 done by Tue ~23:00 IST or we stop.

## 13. Cut list if time runs out (in this order)
Gemini cache/replay -> unfund endpoint -> ledger endpoint -> retries -> event resume (keep snapshot+stream).
Never cut: invariants, funding_need, fund_to_unblock, real PayPal authorize/capture/void.

## 14. Open questions
- Gemini model name and current free-tier limits: verify at M4; keep configurable.
- Does the sandbox accept several partial captures on one authorization in your account? Yes (gate script passed).
