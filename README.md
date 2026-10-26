# CODEMIND

An AI software engineering workspace that understands entire code repositories.

Point it at a public GitHub repository and it indexes the code at a pinned commit, then lets you:

- **Ask questions** and get short answers that cite the exact files and lines they rest on, using free local models through Ollama.
- **Search** the code by keyword, symbol name or meaning.
- **See how the code connects**: which modules import which, and who calls a function and what it calls.
- **Check what a change could break** before you make it, with the tests most likely to cover it.

It has a FastAPI backend with an indexing worker, a React + Vite web app, and Postgres (pgvector) and Redis run with Docker Compose. It is still early; the full plan is laid out in phases in the product blueprint.

## Layout

```
apps/
  api/   FastAPI backend (Python 3.12+)
  web/   React + Vite + Three.js frontend
docs/                Design notes (ingestion, retrieval, graph, impact) and the search evaluation
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

#### On Windows

The same steps work in PowerShell with [Docker Desktop](https://www.docker.com/products/docker-desktop/), Python 3.12+ and Node 20+. Only these commands differ:

```powershell
copy .env.example .env                 # from the repository root
cd apps\api
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1             # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e ".[dev]"
alembic upgrade head
uvicorn codemind.main:app --reload
```

Start the worker with `python worker.py` in a second PowerShell window after running `.venv\Scripts\Activate.ps1` there too. Windows cannot fork, so there the worker runs each job inside its own process instead of a forked child; if a job crashes it, restart the worker; within a few minutes the job is marked `worker_lost` and the repository can be indexed again. The `curl` examples below use bash quoting, so on Windows use the web app or the interactive docs at http://localhost:8000/docs instead. Ollama has a Windows installer at https://ollama.com/download.

If the API, `alembic` or the worker fails with `password authentication failed for user "codemind"`, something other than the compose container is answering on port 5432, usually a PostgreSQL installed directly on Windows. `Get-Process -Id (Get-NetTCPConnection -LocalPort 5432 -State Listen).OwningProcess` shows which program owns the port: `postgres` means a native install, while `com.docker.backend` or `wslrelay` means Docker. For a native install, set `POSTGRES_PORT=5433` in `.env`, change the port in `DATABASE_URL` to 5433, then run `docker compose up -d`. If Docker owns the port, the database volume was created earlier with a different password; `docker compose down -v` followed by `docker compose up -d` recreates it empty.

### Index a repository

```bash
curl -X POST localhost:8000/api/v1/repositories \
  -H 'content-type: application/json' \
  -d '{"url": "https://github.com/owner/repo"}'
```

The response holds the pinned `commit_sha` and a job. Poll `GET /api/v1/jobs/{id}` until it reports `succeeded`, then browse with `GET /api/v1/repositories/{id}/files` and `GET /api/v1/repositories/{id}/files/content?path=...`. The design is in [docs/ingestion-design.md](docs/ingestion-design.md).

To remove a repository and everything indexed from it (snapshots, files, chunks, embeddings, symbols, graph, jobs and past questions), send `DELETE /api/v1/repositories/{id}`. It returns 204, drops queued index jobs, and answers 409 `repository_busy` while an index job is running; cancel that job with `POST /api/v1/jobs/{id}/cancel` and try again once it stops.

To try fetching and filtering without the database or worker, run `python scripts/spike_fetch.py owner/repo`.

### Ask questions about it

Search and answers use free local models served by [Ollama](https://ollama.com); no API keys and no per-call cost. Install Ollama, then pull the two models once (about 6 GB; the answer model wants roughly 8 GB of RAM):

```bash
ollama pull bge-m3            # embeddings for semantic search
ollama pull qwen2.5-coder:7b  # answers
```

Set `EMBEDDING_PROVIDER=ollama` and `LLM_PROVIDER=ollama` in `.env` (see `.env.example`), restart the API and worker, and index (or re-index with `{"force": true}`) so chunks get embeddings. Then:

```bash
curl 'localhost:8000/api/v1/repositories/{id}/search?q=how+are+jobs+retried'
curl -X POST localhost:8000/api/v1/repositories/{id}/investigations \
  -H 'content-type: application/json' \
  -d '{"question": "How are failed indexing jobs retried?"}'
```

An investigation returns a short answer whose claims cite evidence ids; each evidence item has the file path, line range and text from the indexed commit, and claims whose citations do not check out are marked `unverified`. Past questions are listed at `GET /api/v1/repositories/{id}/investigations`. Without Ollama, search still works on keywords and symbol names, and investigations return `answer_model_not_configured`. Code is only sent to the local Ollama server. The design is in [docs/retrieval-design.md](docs/retrieval-design.md).

### See how the code connects

Indexing also builds a code graph for Python, TypeScript and JavaScript: which files import which, which functions call which, and which classes extend which. Each link records the file and line it came from, whether it is `static` (the code names the target directly) or `inferred` (a best guess), and a confidence. Questions about flows ("what happens when `create_order` is called?") use it to pull in the code on the other side of each call.

```bash
curl 'localhost:8000/api/v1/repositories/{id}/symbols?q=place_order'
curl 'localhost:8000/api/v1/repositories/{id}/symbols/{symbol_id}'            # callers and callees
curl 'localhost:8000/api/v1/repositories/{id}/graph/trace?from_symbol_id=...&to_symbol_id=...'
curl 'localhost:8000/api/v1/repositories/{id}/graph/modules?depth=2'          # imports between folders
```

`GET /graph/neighborhood?symbol_id=...` (or `?path=...` for a file's imports) returns the nearby graph as nodes and edges. Repositories indexed before the graph existed report `graph.available: false`; re-index them with `{"force": true}` (in the web app: Re-index, then Re-index anyway). The design, resolution rules and benchmark are in [docs/graph-design.md](docs/graph-design.md).

### Check what a change could break

Give the symbols, files or lines you plan to change, or paste a unified diff (`git diff`), and impact analysis walks the code graph backwards to find what calls, extends or imports them, ranks each dependant by distance and edge confidence, and lists the tests most likely to cover the change. Every result carries the edge and source line it rests on, plus `limitations` saying what static analysis could not see; treat it as advice, not proof.

```bash
curl -X POST localhost:8000/api/v1/repositories/{id}/impact-analysis \
  -H 'content-type: application/json' \
  -d '{"targets": [{"path": "shop/billing.py", "start_line": 1, "end_line": 3}], "depth": 3}'
```

The request can also take `"diff": "<output of git diff>"` and a `symbol_id` in place of a path. The design is in [docs/impact-design.md](docs/impact-design.md).

### Propose a fix and approve it

From an investigation, ask for a change and the local model writes it as exact find-and-replace edits, copied from the code it was shown. The server checks every edit against the indexed commit: the path must stay inside the repository and not be a secret or ignored file, the text to replace must appear exactly once, and the patch must stay within size limits. A patch that passes is `validated` and comes with a unified diff; one that fails is kept as `invalid` with the reason for each edit. You can also send your own `edits` to have them checked without a model.

```bash
curl -X POST localhost:8000/api/v1/investigations/{id}/patches \
  -H 'content-type: application/json' \
  -d '{"instruction": "Retry failed jobs at most three times"}'
curl -X POST localhost:8000/api/v1/patches/{patch_id}/approve \
  -H 'content-type: application/json' -d '{"diff_sha256": "<diff_sha256 from the patch>"}'
curl -X POST localhost:8000/api/v1/patches/{patch_id}/export      # the diff, for git apply
```

Nothing is ever written to a repository. Approval names the SHA-256 of the diff you reviewed, export only works after approval, and every decision is recorded in an audit log. Apply an exported diff yourself with `git apply` on a checkout of the same commit. The design is in [docs/patch-design.md](docs/patch-design.md).

### 3. Run the web app

```bash
cd apps/web
npm install
npm run dev
```

The web app serves on http://localhost:5173. The dev server forwards `/api` and `/health` to the API at http://127.0.0.1:8000 (set `CODEMIND_API_URL` to change it), so start the API and a worker first. The Repositories page imports a GitHub repository and shows its index job's progress; the AI Engineer page asks questions or searches code and opens each cited file at the cited lines; the Architecture page draws which parts of the repository import which (double-click a module to open it) and, for any function or class, who calls it and what it calls, each link opening the line that creates it. For a build served without the dev server, set `VITE_API_URL` to the API's address and add the web origin to the API's `CORS_ORIGINS`.

## Development

From `apps/api`:

```bash
pytest
ruff check .
mypy codemind
```

Database tests run against `TEST_DATABASE_URL` (default: the `codemind_test` database on the local compose Postgres; create it once with `docker compose exec postgres createdb -U codemind codemind_test`). They are skipped when Postgres is not reachable, except in CI (when `CI` is set), where an unreachable database fails the run instead. Each run drops and recreates that database's schema, so never point it at real data.

To measure search quality, `python scripts/evaluate_retrieval.py evaluation/datasets/code-mind.v1.json` runs a fixed set of questions about this repository against an indexed copy of it and reports recall, precision and MRR for keyword, graph and vector retrieval. How it works, how to index the right commit, and the recorded baselines are in [docs/evaluation.md](docs/evaluation.md).

From `apps/web`, `npm run build` type-checks and produces a production build in `dist/`.

## Continuous integration

GitHub Actions (`.github/workflows/ci.yml`) runs on every pull request and on pushes to `main`. The API job runs `ruff check`, `ruff format --check`, `mypy codemind` and `pytest` against a pgvector Postgres service; the web job runs `npm ci` and `npm run build` (TypeScript check plus Vite build).
