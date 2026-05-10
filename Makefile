.DEFAULT_GOAL := help

PY ?= python
UV ?= uv
PYTEST ?= pytest

.PHONY: help install fmt lint test test-unit test-e2e migrate run-master run-agent docker-up docker-down clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "} {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install in dev mode using uv (preferred) or pip
	@if command -v $(UV) >/dev/null 2>&1; then \
		$(UV) sync --extra dev; \
	else \
		$(PY) -m pip install -e ".[dev]"; \
	fi

fmt:  ## Format code with ruff
	$(PY) -m ruff format src tests

lint:  ## Lint with ruff + type-check with mypy
	$(PY) -m ruff check src tests
	$(PY) -m mypy src

test: test-unit  ## Default: unit tests only

test-unit:  ## Run unit tests (no infra required)
	$(PYTEST) -m "not e2e" -v

test-e2e:  ## Run end-to-end tests (requires VAULTMCP_TEST_DATABASE_URL)
	$(PYTEST) -m e2e -v

migrate:  ## Apply schema to $$VAULTMCP_DATABASE_URL
	vaultmcp-master migrate

run-master:  ## Run the master server in foreground
	vaultmcp-master serve

run-agent:  ## Run the agent in foreground
	vaultmcp-agent run

docker-up:  ## Start docker-compose stack (postgres + master + 2 agents)
	cd deploy && docker compose up -d

docker-down:  ## Stop docker-compose stack
	cd deploy && docker compose down

clean:  ## Remove caches, build artifacts, etc.
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
