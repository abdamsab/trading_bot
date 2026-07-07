.PHONY: install install-hub install-gateway dev dev-hub dev-gateway lint test clean audit

# ---------- Installation ----------

install: install-hub install-gateway

install-hub:
	cd hub && pip install -r requirements.txt

install-gateway:
	cd gateway && pip install -r requirements.txt

install-dev:
	pip install -e ".[dev]"

# ---------- Development ----------

dev: dev-hub

dev-hub:
	cd hub && uvicorn app.main:app --reload --port 8000

dev-gateway:
	cd gateway && uvicorn app.main:app --reload --port 9000

# ---------- Quality ----------

lint:
	ruff check hub/ gateway/ shared/
	ruff format --check hub/ gateway/ shared/

format:
	ruff format hub/ gateway/ shared/
	ruff check --fix hub/ gateway/ shared/

test:
	pytest -v --cov=hub --cov=gateway --cov=shared

test-hub:
	pytest -v hub/tests/

test-gateway:
	pytest -v gateway/tests/

# ---------- Security ----------

audit:
	pip-audit

# ---------- Clean ----------

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.db" -delete
	rm -rf .coverage htmlcov/
