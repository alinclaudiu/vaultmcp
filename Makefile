.DEFAULT_GOAL := help

# Default to python3 — Debian 13 / Ubuntu 24.04 / most modern distros
# don't ship a `python` command. Override with PY=python if you have one.
PY ?= python3
UV ?= uv
VENV ?= .venv
VENV_BIN := $(VENV)/bin
PYTEST ?= $(VENV_BIN)/pytest

.PHONY: help bootstrap install install-uv install-venv fmt lint test test-unit test-e2e \
        migrate run-master run-agent docker-up docker-down clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "} {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ----- install pipeline -----

install:  ## Install dev deps (auto-detects uv vs pip+venv)
	@if command -v $(UV) >/dev/null 2>&1; then \
		echo "==> Using uv"; \
		$(MAKE) install-uv; \
	else \
		echo "==> uv not found; falling back to python3 + venv"; \
		$(MAKE) install-venv; \
	fi
	@echo
	@echo "Done. To activate the environment in your shell:"
	@if command -v $(UV) >/dev/null 2>&1; then \
		echo "    source $(VENV_BIN)/activate"; \
	else \
		echo "    source $(VENV_BIN)/activate"; \
	fi

install-uv:  ## Install via uv (creates and manages .venv automatically)
	$(UV) sync --extra dev

install-venv:  ## Install via stdlib venv + pip (PEP 668 friendly)
	@if [ ! -d "$(VENV)" ]; then \
		echo "==> Creating $(VENV) with $(PY)"; \
		$(PY) -m venv $(VENV); \
	fi
	@echo "==> Upgrading pip in $(VENV)"
	@$(VENV_BIN)/python -m pip install --quiet --upgrade pip
	@echo "==> Installing vaultmcp[dev] in $(VENV)"
	$(VENV_BIN)/python -m pip install -e ".[dev]"

bootstrap:  ## One-shot: install uv if missing, then `make install`
	@if ! command -v $(UV) >/dev/null 2>&1; then \
		echo "==> Installing uv via pipx (or curl)"; \
		if command -v pipx >/dev/null 2>&1; then \
			pipx install uv; \
		else \
			curl -LsSf https://astral.sh/uv/install.sh | sh; \
			echo "Re-source your shell or add ~/.local/bin to PATH, then run: make install"; \
			exit 0; \
		fi; \
	fi
	@$(MAKE) install

# ----- dev tools (run inside the venv) -----

fmt:  ## Format code with ruff
	$(VENV_BIN)/ruff format src tests

lint:  ## Lint with ruff + type-check with mypy
	$(VENV_BIN)/ruff check src tests
	$(VENV_BIN)/mypy src

test: test-unit  ## Default: unit tests only

test-unit:  ## Run unit tests (no infra required)
	$(PYTEST) -m "not e2e" -v

test-e2e:  ## Run end-to-end tests (requires VAULTMCP_TEST_DATABASE_URL)
	$(PYTEST) -m e2e -v

# ----- runtime -----

migrate:  ## Apply schema to $$VAULTMCP_DATABASE_URL
	$(VENV_BIN)/vaultmcp-master migrate

run-master:  ## Run the master server in foreground
	$(VENV_BIN)/vaultmcp-master serve

run-agent:  ## Run the agent in foreground
	$(VENV_BIN)/vaultmcp-agent run

docker-up:  ## Start docker-compose stack (postgres)
	cd deploy && docker compose up -d

docker-down:  ## Stop docker-compose stack
	cd deploy && docker compose down

clean:  ## Remove caches, build artifacts, etc.
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +

clean-venv:  ## Remove the .venv directory (start fresh)
	rm -rf $(VENV)
