# Videre

**See, and budget, what an AI agent does.**

You fund an agent's wallet with a PayPal *authorization*. The agent plans a tree of tasks and works
through it, and each task runs only while it has money. A live graph shows every task, call, thought
and unit of spend. A stalled task can be funded with one click (together with the unfunded tasks it
depends on). Closing the run captures what was spent and voids the rest back to you.

Built for the PayPal AI Hackathon (November 2026). Licence: Apache-2.0.

## Status

| Milestone | What | State |
| --- | --- | --- |
| A0 | Monorepo scaffold, config, schema, Store, health route, CI | done |
| A1 | Events, SSE stream, snapshot and replay | next |
| A2 | Agent core: planner, executor, mock tools, LLM cache | planned |
| A3 | Frontend live graph | planned |
| B | Budget engine, funding, PayPal authorize / capture / void, close | planned |

Today the app starts, serves `GET /api/health`, and the frontend shows that status. Nothing else
is built yet. The plan is in [docs/PLAN-backend.md](docs/PLAN-backend.md).

## Quickstart

Needs [uv](https://docs.astral.sh/uv/), Node 22+ and GNU Make.

```bash
cp .env.example .env   # optional: the defaults run on the mock rail with no keys
make dev               # backend on :8000, frontend on :5173
```

| Command | Does |
| --- | --- |
| `make dev` | install from the lockfiles, then run backend and frontend together |
| `make test` | pytest (backend) and Vitest (frontend) |
| `make lint` | ruff check and format check, ESLint, `tsc` |
| `make fmt` | auto-format and auto-fix |
| `make demo` | scripted demo run (arrives with milestone A2) |

The backend runs in its own virtualenv, `backend/.venv`, which `uv` creates from `backend/uv.lock`.
Tests that touch the PayPal sandbox or Gemini carry the `live` marker and are skipped unless you
run `uv run pytest --live` from `backend/`.

## Layout

```
backend/src/videre/   api -> services -> domain / storage   (domain never does I/O)
backend/tests/        unit/ and integration/
frontend/src/         api, features/{graph,node-panel,prompt,wallet}, components, hooks, lib, types
docs/                 plan, architecture, decision records (adr/)
scripts/              paypal_gate_check.py: the sandbox go/no-go check for the wallet mechanic
```

More in [docs/architecture.md](docs/architecture.md).

## Honest limits

- **PayPal sandbox only.** The backend refuses to start with `RAIL=paypal` unless the base URL is
  the sandbox host.
- **LLM spend is metered, not charged.** Gemini calls run on the free tier and are priced from
  token counts at list price. That figure is never real card spend.
- **Money is integer micro-USD** (1 USD = 1,000,000) end to end. No floats.
- Single user, local only: no auth, no deployment.
