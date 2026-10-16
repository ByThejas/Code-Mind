# CODEMIND

An AI software engineering workspace that understands entire code repositories.

This repository is early: a FastAPI backend that can import a public GitHub repository at a pinned commit and index it into Postgres, a React + Vite web shell, and local Postgres (pgvector) and Redis services. The full plan is laid out in phases in the product blueprint.

## Layout

```
apps/
  api/   FastAPI backend (Python 3.12+)
  web/   React + Vite + Three.js frontend
docs/                Design notes (see ingestion-design.md)
docker-compose.yml   Postgres 16 with pgvector, Redis 7
```

## Getting started

### 1. Start the databases

```bash
docker compose up -d
```

Postgres listens on `localhost:5432` and Redis on `localhost:6379`. Override the defaults with `POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD` in a `.env` file at the repository root (it is git-ignored).

### 2. Run the API

```bash
cd apps/api
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn codemind.main:app --reload
```

In a second terminal (same folder and virtualenv), start the indexing worker:

```bash
python worker.py
```

The API serves on http://localhost:8000. Try `GET /health`, or open http://localhost:8000/docs.

Settings are read from the root `.env` file; copy `.env.example` to start. `GITHUB_TOKEN` is optional: public repositories work without it, but GitHub then allows only 60 API requests an hour. See `apps/api/codemind/core/config.py` for every setting and its default.

### Index a repository

```bash
curl -X POST localhost:8000/api/v1/repositories \
  -H 'content-type: application/json' \
  -d '{"url": "https://github.com/owner/repo"}'
```

The response holds the pinned `commit_sha` and a job. Poll `GET /api/v1/jobs/{id}` until it reports `succeeded`, then browse with `GET /api/v1/repositories/{id}/files` and `GET /api/v1/repositories/{id}/files/content?path=...`. The design is in [docs/ingestion-design.md](docs/ingestion-design.md).

To try fetching and filtering without the database or worker, run `python scripts/spike_fetch.py owner/repo`.

### 3. Run the web app

```bash
cd apps/web
npm install
npm run dev
```

The web app serves on http://localhost:5173.

## Development

From `apps/api`:

```bash
pytest
ruff check .
mypy codemind
```

Database tests run against `TEST_DATABASE_URL` (default: the `codemind_test` database on the local compose Postgres; create it once with `docker compose exec postgres createdb -U codemind codemind_test`). They are skipped when Postgres is not reachable, except in CI (when `CI` is set), where an unreachable database fails the run instead. Each run drops and recreates that database's schema, so never point it at real data.

From `apps/web`, `npm run build` type-checks and produces a production build in `dist/`.

## Continuous integration

GitHub Actions (`.github/workflows/ci.yml`) runs on every pull request and on pushes to `main`. The API job runs `ruff check`, `ruff format --check`, `mypy codemind` and `pytest` against a pgvector Postgres service; the web job runs `npm ci` and `npm run build` (TypeScript check plus Vite build).
