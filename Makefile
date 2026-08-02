.DEFAULT_GOAL := help
.PHONY: help install lint format format-check unit integration coverage ci \
	redis redis-stop docker-build docker-up docker-down smoke clean

help: ## Show this help (list of targets + descriptions)
	@echo "Available targets:"
	@grep -E '^[a-zA-Z0-9_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*##"} {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install/sync dependencies from the lockfile (matches CI's `uv sync --locked`)
	uv sync --locked

lint: ## Run ruff static analysis (app/, tests/, scripts/) — no formatting/mutation
	uv run ruff check .

format: ## Auto-format the codebase with ruff
	uv run ruff format .

format-check: ## Check formatting without modifying files (CI-safe)
	uv run ruff format --check .

unit: ## Run tests/unit — fast, no external services required
	uv run pytest tests/unit -q

integration: ## Run tests/integration — real-socket ASGI backends (real-Redis case skips without AAC_TEST_REDIS_URL, default redis://localhost:6379/15)
	uv run pytest tests/integration -q

coverage: ## Run the full test suite with a coverage report (term-missing)
	uv run pytest --cov=app --cov-report=term-missing

ci: lint unit integration ## Run lint + unit + integration, in that order (mirrors .github/workflows/ci.yml)

redis: ## Start a disposable Redis container on :6379 for real-Redis integration tests
	docker run --rm -d --name aac-test-redis -p 6379:6379 redis:7-alpine

redis-stop: ## Stop the disposable Redis container started by `make redis`
	docker stop aac-test-redis

docker-build: ## Build the AAC image via Docker Compose
	docker compose build

docker-up: ## Start the full stack (AAC + Redis) via Docker Compose, in the foreground
	docker compose up --build

docker-down: ## Stop and remove the Docker Compose stack
	docker compose down

smoke: ## Run the automated failover/failback Docker Compose smoke test
	./scripts/smoke_test_failover.sh

clean: ## Remove Python/pytest/ruff cache directories
	find . -type d -name '__pycache__' -not -path './.venv/*' -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
