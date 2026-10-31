PY ?= python3
PORT ?= 8000

.PHONY: help install run dev test lint format chaos run-chaos demo bench docker docker-chaos clean

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

install:         ## create .venv and install runtime + dev dependencies
	$(PY) -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

run:             ## run the API on :8000 (syncs from the live Elanco API)
	.venv/bin/uvicorn app.main:app --port $(PORT)

dev:             ## run with auto-reload and human-readable logs
	LOG_JSON=false .venv/bin/uvicorn app.main:app --port $(PORT) --reload

test:            ## run the test suite
	.venv/bin/pytest -q

lint:            ## static checks
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

format:          ## auto-format
	.venv/bin/ruff format . && .venv/bin/ruff check --fix .

chaos:           ## start the misbehaving upstream on :9000
	.venv/bin/python -m tools.chaos_upstream --port 9000 --preset chaos

run-chaos:       ## run the API on :8001 against the chaos upstream (separate DB)
	UPSTREAM_BASE_URL=http://127.0.0.1:9000/data DATABASE_URL=sqlite:///data/chaos.db \
	SYNC_INTERVAL_MINUTES=1 LOG_JSON=false .venv/bin/uvicorn app.main:app --port 8001

demo:            ## scripted tour of the API with curl (server must be running)
	./scripts/demo.sh http://127.0.0.1:$(PORT)

bench:           ## ingest 200k synthetic rows and time the heavy queries
	.venv/bin/python -m scripts.benchmark --rows 200000

docker:          ## build and run with Docker
	docker compose up --build

docker-chaos:    ## API + chaos upstream + API-against-chaos, all in Docker
	docker compose --profile chaos up --build

clean:           ## remove local databases and caches
	rm -rf data/*.db data/*.db-* .pytest_cache .ruff_cache **/__pycache__
