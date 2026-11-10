.DEFAULT_GOAL := help
.PHONY: help install dev dev-backend dev-frontend test lint fmt demo

help: ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/'

install: ## install backend and frontend dependencies from the lockfiles
	cd backend && uv sync --locked
	cd frontend && npm ci

dev: install ## run backend (:8000) and frontend (:5173) together
	@$(MAKE) -j2 dev-backend dev-frontend

dev-backend:
	cd backend && uv run uvicorn videre.main:app --factory --reload --port 8000

dev-frontend:
	cd frontend && npm run dev

test: ## run backend and frontend tests
	cd backend && uv run pytest
	cd frontend && npm test

lint: ## ruff, eslint and type checks (no changes written)
	cd backend && uv run ruff check . && uv run ruff format --check .
	cd frontend && npm run lint && npm run typecheck

fmt: ## auto-format and auto-fix
	cd backend && uv run ruff format . && uv run ruff check --fix .
	cd frontend && npm run lint -- --fix

demo: ## run the demo scenario (arrives with milestone A2)
	@echo "make demo is not implemented yet: the agent run lands in milestone A2."; exit 1
