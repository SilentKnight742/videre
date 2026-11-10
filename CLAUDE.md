# Videre

See, and budget, what an AI agent does. A user funds an agent wallet (PayPal authorization); the agent runs a task graph, spending metered money per task; a live graph shows where every unit went; stalled tasks can be funded (with their unfunded dependencies); closing the run captures spend and voids the rest.

- Plan to follow: docs/PLAN-backend.md. Read the REVISION 1 block at the top first: it overrides the milestone order (visualizer phase A first, money phase B second). Do one milestone per session, meet its acceptance criteria, commit.
- Deadline: Fri 13 Nov 2026 01:30 IST. Licence: Apache-2.0.
- Money is integer micro-USD, never floats. Never log secrets. PayPal sandbox only.
- LLM spend is metered at list price (Gemini free tier); never present it as real card spend.
- Do not add scope beyond the plan; if something must be cut, follow section 13.
- Repo layout and conventions: see section 2 of the plan (backend/src/videre, frontend/src, docs/adr, Makefile, CI). Conventional Commits; keep main green; move scripts/paypal_gate_check.py with git mv.
- Git: never commit or push directly to main. One branch per milestone (feat/a0-scaffold etc.), Conventional Commits, `make lint test` before a PR, open a PR with `gh pr create`, squash-merge after CI passes, delete the branch, pull main. Never force-push, never --no-verify. Full rules: "Git workflow" in docs/PLAN-backend.md section 2.
