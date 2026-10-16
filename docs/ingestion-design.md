# Repository Ingestion — Design (Phase 1 spike → Phase 3)

Status: implemented in apps/api (first slice) · 2026-10-16 · Source: CODEMIND blueprint v1.0 §3.1, §8, §9, §14, §20 (Phases 1 and 3)

## 1. Goal and scope

Import a **public GitHub repository at a pinned commit**, filter its files safely, and store files, chunks and
(placeholder) symbols in Postgres so later phases can search and cite them. Exit gate (blueprint Phase 3 / M2):
a sample repository indexes repeatably, records its commit SHA, never mixes commits, and does not duplicate rows on retry.

In scope: URL validation, ref → SHA resolution, tarball fetch, file filtering, secret-path exclusion, line-based and
Tree-sitter chunking for Python and TS/JS, job queue with progress/retry/cancel, first REST endpoints.

Out of scope (later phases): private repos/OAuth (Beta), embeddings generation (Phase 4 — table created now, filled
later), symbol graph edges (Phase 5), incremental re-indexing (§9.2), auth (a single seeded workspace until auth lands).

## 2. Starting point (current `apps/api`)

- FastAPI app with `/` and `/health` (`codemind/main.py`); `api/v1/health.py` and `tests/test_health.py` are empty.
- `core/config.py` already has `database_url` (psycopg 3) and `redis_url`; deps include SQLAlchemy 2, redis, httpx.
- `docker-compose.yml` runs `pgvector/pgvector:pg16` and `redis:7-alpine`.
- Missing and added by this work: Alembic, a queue library, a worker entry point, Tree-sitter, models, routes.

## 3. Key decisions

| Decision | Choice | Why |
|---|---|---|
| Queue | **RQ** (not Celery) | Blueprint says pick one. RQ is plain Redis, sync, small; enough for one worker type. Revisit if we need chords/rate limits. |
| Fetch method | **GitHub tarball API at a SHA** (`GET /repos/{o}/{r}/tarball/{sha}`) via httpx, not `git clone`; files are read straight from the archive stream and never extracted | No git binary or `.git` history in the worker, one HTTP request, size can be capped while streaming, no hooks run, and nothing can be written outside a workspace. |
| Pinning | Resolve ref → SHA first (`GET /repos/{o}/{r}/commits/{ref}`), store SHA, fetch by SHA only | Snapshot consistency: a moving branch never leaks into an index. |
| Source storage | Store file **text in Postgres** (`file_contents`), discard tarball after indexing | Citations and the file viewer need exact lines; avoids object storage in the MVP. Revisit with S3 if DB size hurts. |
| Snapshot identity | `UNIQUE (repository_id, commit_sha)` | Re-importing the same commit reuses the snapshot instead of duplicating it. |
| Embedding dimension | Fixed per deployment via `EMBEDDING_DIMENSIONS` (default 1024), recorded per row | pgvector HNSW needs a fixed dimension; changing model = migration + re-embed, which the blueprint already expects. |
| Migrations | Alembic, first migration runs `CREATE EXTENSION IF NOT EXISTS vector` | Blueprint stack; the pgvector image ships the extension but doesn't enable it. |

## 4. Data model

Subset of blueprint §8 needed now. All ids are UUIDs; all timestamps `timestamptz`. Everything below `repositories`
cascades on delete (supports FR-12 later).

```text
workspaces              id, name, created_at                       -- one seeded "default" row until auth
repositories            id, workspace_id FK, provider ('github'), owner, name, default_branch,
                        visibility, created_at
                        UNIQUE (workspace_id, provider, lower(owner), lower(name))
repository_snapshots    id, repository_id FK, commit_sha char(40), requested_ref,
                        status, stats jsonb, warnings jsonb, created_at, indexed_at
                        UNIQUE (repository_id, commit_sha)
files                   id, snapshot_id FK, path, language, size_bytes, content_hash (sha256),
                        line_count, excluded_reason NULL
                        UNIQUE (snapshot_id, path)
file_contents           file_id PK/FK, text                          -- only for included files
chunks                  id, file_id FK, snapshot_id FK (denormalised for filtering), ordinal,
                        start_line, end_line, kind ('symbol' | 'lines'), symbol_name NULL,
                        text, token_estimate, search_tsv tsvector GENERATED
                        UNIQUE (file_id, ordinal); GIN index on search_tsv
symbols                 id, file_id FK, snapshot_id FK, name, qualified_name, kind,
                        start_line, end_line, parent_symbol_id NULL
embeddings              chunk_id PK/FK, model_name, dimensions, embedding vector(N), created_at
                        HNSW index (vector_cosine_ops)                -- filled in Phase 4
jobs                    id, workspace_id FK, snapshot_id FK NULL, type ('index_snapshot'),
                        status, stage, progress (0–100), attempt, max_attempts,
                        cancel_requested bool, error_code, error_message,
                        created_at, started_at, finished_at
                        UNIQUE (snapshot_id) WHERE status IN ('queued','running')
                        -- the RQ job id is the same UUID as jobs.id
```

**Snapshot status:** `queued → fetching → indexing → ready | partial | failed | cancelled`.
`partial` means the job finished but skipped files for limit/parse reasons; `warnings` says which and why.

**Job status:** `queued → running → succeeded | failed | cancelled`, with `stage` naming the current pipeline step
(`resolve`, `fetch`, `filter`, `parse`, `store`, `finalize`) so the UI can show real progress.

Excluded files are still written to `files` with `excluded_reason` (`binary`, `too_large`, `ignored_path`,
`secret_path`, `secret_content`, `binary`, `generated`, `limit_reached`, `unsupported`) and no content. That gives FR-02's "logged with reasons".

## 5. Ingestion job flow

```text
POST /repositories ──► validate URL ──► upsert repository
                                    └─► resolve ref → SHA (GitHub API)
                                    └─► upsert snapshot (repo, sha)  ── exists & ready? return it, no job
                                    └─► insert job (idempotency_key = "index:{snapshot_id}")
                                    └─► rq.enqueue(index_snapshot, job_id)           ──► 202 {repository, snapshot, job}

worker: index_snapshot(job_id)
  1 resolve   load job+snapshot; bail if cancel_requested; mark running
  2 fetch     stream tarball by SHA to a temp dir; abort past MAX_INDEX_BYTES / MAX_JOB_SECONDS
  3 filter    read members from the archive stream (skip absolute paths and ".."; symlinks and
              devices are recorded as `unsupported`); apply rules (§6)
  4 parse     per included file: decode UTF-8 (else binary), detect language, Tree-sitter → symbols;
              build chunks (symbol chunks, else ~60-line windows with 10-line overlap)
  5 store     in one transaction per batch of ~200 files: DELETE existing rows for snapshot, then INSERT
  6 finalize  set snapshot ready/partial, stats, indexed_at; job succeeded; delete temp dir
```

**Idempotency.** Each run starts by deleting the snapshot's existing `files` (cascade to contents/chunks/symbols),
so a retry or a manual re-index rewrites, never duplicates. A partial unique index allows only one queued or
running job per snapshot, so two concurrent `POST`s for the same commit get the same job back instead of racing.

**Retries.** RQ `Retry(max=3, interval=[10, 60, 300])` for transient errors only (network, GitHub 5xx, 429 with
`Retry-After`). Validation errors (404 repo, over-limit repo) fail immediately with a user-facing `error_code`.
Failed jobs land in RQ's failed registry, which is our dead-letter queue.

**Cancellation.** `POST /jobs/{id}/cancel` sets `cancel_requested`; the worker checks it between stages and every
file batch, then marks job and snapshot `cancelled` and cleans up. Queued jobs are also removed from RQ.

**Crashed workers.** While a job runs, a background thread in the worker writes `jobs.heartbeat_at` every
`JOB_HEARTBEAT_SECONDS` (30). A `running` job with no heartbeat for `JOB_STALE_SECONDS` (300) is treated as lost:
it is marked `failed` with `error_code = worker_lost`, its snapshot is cleared and marked `failed`, and the commit
can be indexed again. Stale jobs are reaped when someone re-imports or re-indexes that commit, when
`GET /jobs/{id}` reads one, and when a worker starts. Times come from the database clock. If a reaped worker was
only slow, it sees at its next checkpoint that it no longer owns the job and stops without writing.

**Progress.** The worker updates `jobs.stage/progress` at most once per second (files processed / files total
during parse+store). The UI polls `GET /jobs/{id}` (TanStack Query, ~2 s); SSE can come later if polling feels slow.

**Concurrency.** One RQ queue `indexing`, worker count set by deployment (start with 1). Job timeout = `MAX_JOB_SECONDS`.

## 6. Validation, limits and filtering

URL validation (SSRF row in blueprint §16): accept only `https://github.com/{owner}/{repo}[.git][/tree/{ref}]` or
`owner/repo`; owner/repo match `^[A-Za-z0-9_.-]{1,100}$`. The worker only ever talks to `api.github.com` and
`codeload.github.com`, so user input never becomes a hostname. Redirects are followed only to those two hosts.

Repo-level checks before fetching: repo exists and `private == false`; GitHub `size` (KB) under `MAX_INDEX_BYTES`.

Limits (new settings, defaults from blueprint §18.3): `MAX_REPOSITORY_FILES=20000`, `MAX_FILE_BYTES=1000000`,
`MAX_INDEX_BYTES=500000000`, `MAX_JOB_SECONDS=1800`. Hitting the file-count limit marks remaining files
`limit_reached` and the snapshot `partial`.

Filter rules, applied server-side regardless of `.gitignore`:

- Ignored dirs: `.git`, `node_modules`, `dist`, `build`, `out`, `vendor`, `.venv`, `venv`, `__pycache__`, `.next`,
  `coverage`, `target`, `.cache`, `.idea`, `.vscode`.
- Generated: `*.min.js`, `*.map`, lockfiles (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`) —
  kept in `files` for listing, excluded from chunking.
- Secret paths (never stored as content): `.env*` (except `.env.example`), `*.pem`, `*.key`, `id_rsa*`, `*.p12`,
  `*.pfx`, `credentials*.json`, `.npmrc`, `.pypirc`, `secrets.*`.
- Content secret scan: a small regex set (AWS keys, GitHub tokens, private-key headers, generic `api_key=`) run on
  every included file; a hit excludes the file with `secret_content`. Replace with `detect-secrets` if it proves noisy.
- Binary: NUL byte in the first 8 KB, or not valid UTF-8.
- No install scripts, builds or repo code are ever executed during indexing.

Languages for parsing (blueprint baseline): Python, TypeScript, TSX, JavaScript via `tree-sitter` +
`tree-sitter-python` / `tree-sitter-typescript` / `tree-sitter-javascript`. Everything else that is text gets
line-window chunks only. A parse error downgrades that file to line chunks and adds a warning; it never fails the job.

## 7. First API endpoints

All under `/api/v1`, Pydantic schemas, errors as `{"error": {"code", "message", "request_id"}}`.
Every query is scoped by `workspace_id` (the seeded default for now, a dependency `get_current_workspace` so auth
can replace it without touching routes).

| Method + path | Purpose | Response |
|---|---|---|
| `POST /repositories` | Body `{url, ref?}`. Validate, resolve SHA, create snapshot + job. | `202 {repository, snapshot, job}`; `200` if that commit is already `ready` |
| `GET /repositories` | List repositories in the workspace with latest snapshot status. | `200 {items, next_cursor}` |
| `GET /repositories/{id}` | Metadata, latest snapshot (SHA, status, stats, warnings). | `200` / `404` |
| `POST /repositories/{id}/index` | Body `{ref?}`. Re-index default branch head or a ref; idempotent per SHA. | `202 {snapshot, job}` |
| `GET /jobs/{id}` | Status, stage, progress, safe error code/message. | `200` |
| `POST /jobs/{id}/cancel` | Request cancellation. | `202` |
| `GET /repositories/{id}/files` | `?snapshot_id&path_prefix&include_excluded&cursor`. Paginated file list. | `200 {items, next_cursor}` |
| `GET /repositories/{id}/files/content` | `?snapshot_id&path&start_line&end_line`. Capped at 2,000 lines. | `200 {path, commit_sha, start_line, end_line, text}` |

`snapshot_id` defaults to the latest `ready`/`partial` snapshot. Responses always carry `commit_sha` so the UI
can show which revision it is looking at (FR-01). `DELETE /repositories/{id}` is listed in the blueprint and is cheap
given the cascades, but is left for the next slice.

Error codes for the first slice: `invalid_repository_url`, `repository_not_found`, `repository_private`,
`repository_too_large`, `ref_not_found`, `github_rate_limited`, `fetch_failed`, `job_timeout`, `cancelled`.

## 8. Code layout

Follows blueprint §7; only what this work touches:

```text
apps/api/
  codemind/
    api/dependencies.py, api/v1/router.py, api/v1/routes/{repositories,jobs,files}.py
    core/config.py            + github_token (optional), limits, embedding_dimensions
    core/errors.py, core/handlers.py   error type, request ids, {"error": ...} responses
    db/base.py, db/models.py, db/session.py, db/migrations/ (Alembic, 0001_ingestion_tables)
    repositories/github.py    URL parsing, GitHub client (metadata, resolve ref, tarball stream)
    repositories/service.py   import / re-index: upsert repository and snapshot, one active job
    indexing/archive.py       read and filter files from the tarball; indexing/filters.py rules
    indexing/chunking.py, indexing/pipeline.py (stages 1–6), indexing/queue.py, indexing/tasks.py
    parsing/treesitter.py     symbol extraction for Python, TS, TSX, JS
    schemas/ingestion.py      Pydantic request/response models
  worker.py                   RQ worker on the indexing queue (with scheduler, for retries)
  scripts/spike_fetch.py      Phase 1 spike
  tests/fixtures.py           builds tarballs: generated dirs, binary, oversized, .env, malformed .py, symlink, traversal
```

New dependencies: `alembic`, `rq`, `pgvector` (SQLAlchemy type), `tree-sitter`, `tree-sitter-python`,
`tree-sitter-typescript`, `tree-sitter-javascript`. An optional `GITHUB_TOKEN` raises the API limit from 60 to
5,000 requests/hour; it is read-only, never logged, and not required for public repos.

## 9. Phase 1 spike (do first, 1–2 sessions)

A script `scripts/spike_fetch.py owner/repo [ref]` that resolves the SHA, streams the tarball, reads it
without extracting, applies the filters and prints counts per `excluded_reason` plus Tree-sitter symbol counts per language.
Run it on 2–3 demo repos (one Python, one TS, one mixed). It answers: is the tarball route fast enough, do the
limits look right, and do the Tree-sitter grammars install cleanly on Python 3.12. Its filter and parse functions then
move into `indexing/` unchanged.

Result (2026-10-16): on ByThejas/Code-Mind, 38 KB tarball in 0.6 s, 24 files, 72 chunks. On a 9.8 MB
archive of FastAPI, Starlette, Pydantic and SQLAlchemy sources, 466 files, 17,112 Python symbols and
9,542 chunks were scanned and parsed in 3.5 s. Grammars installed cleanly on Python 3.13. The sandbox could
only reach this one repository, so other demo repositories still need a run.

## 10. Testing

- Unit: URL parsing (valid forms, SSRF-ish inputs like `github.com.evil.com`, `../`), filter rules per fixture,
  safe extraction (path traversal and symlink tarballs), chunk boundaries, binary detection.
- Integration (compose Postgres + Redis, GitHub mocked with `respx`): full job on a fixture tarball writes the expected
  rows; running it twice gives identical row counts; cancel mid-run ends `cancelled` and its snapshot is never served by the file endpoints;
  a 503 then 200 from GitHub retries and succeeds.
- API: 202/200 behaviour on duplicate imports, 404 for another workspace's repository id.

## 11. Open questions

Decided 2026-10-16: the server uses an optional `GITHUB_TOKEN` when one is set (question 3).

1. Embedding provider and dimension. Affects only the `embeddings` column; can be decided in Phase 4.
2. Do we keep old snapshots when a repo is re-indexed at a new SHA? Proposal: keep the last 3, delete older ones.
3. ~~Is a GitHub token for the server acceptable for the MVP?~~ Yes, optional, used only when set.
