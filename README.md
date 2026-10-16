# CODEMIND

An AI software engineering workspace that understands entire code repositories.

This repository is an early skeleton: a FastAPI backend with a health endpoint, a React + Vite web shell, and local Postgres (pgvector) and Redis services. The full plan is laid out in phases in the product blueprint.

## Layout

```
apps/
  api/   FastAPI backend (Python 3.12+)
  web/   React + Vite + Three.js frontend
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
uvicorn codemind.main:app --reload
```

The API serves on http://localhost:8000. Try `GET /health`, or open http://localhost:8000/docs.

Settings are read from the root `.env` file (`DATABASE_URL`, `REDIS_URL`, `CORS_ORIGINS`, `DEBUG`); see `apps/api/codemind/core/config.py` for the defaults.

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

From `apps/web`, `npm run build` type-checks and produces a production build in `dist/`.
