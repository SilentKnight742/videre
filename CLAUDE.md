# Videre

See, and budget, what an AI agent does. A user funds an agent wallet (PayPal authorization); the agent runs a task graph, spending metered money per task; a live graph shows where every unit went; stalled tasks can be funded (with their unfunded dependencies); closing the run captures spend and voids the rest.

## What to read
- Current milestone spec: docs/SPEC-A2.md (agent core). It overrides docs/PLAN-backend.md where they disagree.
- Background and phase B (money): docs/PLAN-backend.md. Its REVISION 1 block sets the order: visualizer (phase A) first, money (phase B) second.
- Do one PR-sized piece per session (SPEC-A2 section 11 lists them), meet its acceptance criteria, then stop.

## Rules
- Quality over speed. Licence: Apache-2.0.
- Money is integer micro-USD, never floats. Never log secrets. PayPal sandbox only.
- LLM spend is metered at list price (Gemini free tier); never present it as real card spend.
- Tests never touch the network (use FakeProvider); real-API tests are marked `live` and skipped by default.
- Do not add scope beyond the spec. If the spec is wrong or unclear, stop and say so instead of improvising.
- Ask before installing, downloading or upgrading any tool, package or binary that is not already a declared project dependency.
- Repo layout and conventions: PLAN-backend.md section 2 (backend/src/videre, frontend/src, docs/adr, Makefile, CI).

## Git
- Start every session with `git checkout main && git pull`.
- Never commit or push directly to main. One branch per piece of work (feat/..., fix/..., docs/...), Conventional Commits.
- `make lint` and `make test` must pass before opening a PR with `gh pr create`.
- Stop after opening the PR. Never merge it: the user reviews and merges. Start the next piece only after the user says main is updated.
- Never force-push, never rewrite main, never `--no-verify`. If a merge or rebase conflict is unclear, stop and ask.
