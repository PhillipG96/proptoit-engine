# PropToIt — open-source sample stack.
#
#   make dev     boot postgres + meilisearch, apply schema, load sample data
#   make index   index properties into meilisearch (collectors/index_to_meili.py)
#   make clean   stop and remove containers + volumes
#
# The Makefile is the ONLY place that hardcodes host ports (5437 postgres,
# 7707 meilisearch) — 5432/5433/7700 are reserved for other local stacks.
# Scripts read config from the environment instead.
#
# `make dev` generates .env with fresh random secrets (never committed;
# see .env.example). Override host ports in .env if 5437/7707 are taken.

POSTGRES_HOST_PORT ?= 5437
MEILI_HOST_PORT    ?= 7707

PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

export POSTGRES_HOST_PORT MEILI_HOST_PORT

.PHONY: dev venv index clean ps logs

# Bootstrap a venv with the project's own requirements (api + indexing +
# sample loader). No-op when .venv already has the deps.
venv:
	@test -x .venv/bin/python || python3 -m venv .venv
	@$(PY) -m pip install -q -r requirements.txt

dev: venv
	@if [ ! -f .env ]; then \
		echo "==> Generating .env with random secrets"; \
		printf 'POSTGRES_PASSWORD=%s\nMEILI_MASTER_KEY=%s\n' \
			"$$(openssl rand -hex 32)" "$$(openssl rand -hex 32)" > .env; \
	fi
	@set -a; . ./.env; set +a; \
	docker compose up -d --wait; \
	until docker compose exec -T postgres pg_isready -U proptoit -d proptoit -q; do sleep 1; done; \
	echo "==> Applying schema (db/init.sql + migrations)"; \
	docker compose exec -T postgres psql -U proptoit -d proptoit -v ON_ERROR_STOP=1 < db/init.sql; \
	for m in db/migrations/*.sql; do \
		echo "==> Applying $$m"; \
		docker compose exec -T postgres psql -U proptoit -d proptoit -v ON_ERROR_STOP=1 < "$$m"; \
	done; \
	echo "==> Loading sample data"; \
	DATABASE_URL="postgresql://proptoit:$${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing - run make dev first}@localhost:$${POSTGRES_HOST_PORT}/proptoit" \
		$(PY) scripts/load_sample.py; \
	echo; echo "Sample stack ready:"; \
	echo "  postgres:    localhost:$(POSTGRES_HOST_PORT) (db proptoit)"; \
	echo "  meilisearch: localhost:$(MEILI_HOST_PORT)"; \
	echo "Next: make index"

# Export .env vars for the python targets without printing them.
index: venv
	@set -a; . ./.env; set +a; \
	DATABASE_URL="postgresql://proptoit:$${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing - run make dev first}@localhost:$${POSTGRES_HOST_PORT}/proptoit" \
	MEILI_URL="http://localhost:$(MEILI_HOST_PORT)" \
		$(PY) collectors/index_to_meili.py

clean:
	docker compose down -v

ps:
	docker compose ps

logs:
	docker compose logs -f
