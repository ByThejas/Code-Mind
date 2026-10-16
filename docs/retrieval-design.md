# Retrieval and Grounded Q&A — Design (Phase 4)

Status: implemented in apps/api (first slice) · 2026-10-16 · Source: CODEMIND blueprint v1.0 §3.2, §9.1, §10, §12, §14 (Phase 4)

## 1. Goal and scope

Answer a natural-language question about an indexed snapshot with a short, structured answer whose every claim
points at real file/line evidence from that exact commit. Exit gate (blueprint Phase 4): questions can be answered
with inspectable evidence and clear limitations.

In scope: chunk embeddings during indexing, keyword + symbol + vector retrieval, rank fusion and de-duplication,
context assembly under a token budget, a structured answer from one model call, citation validation, stored
investigations (history and audit metadata, FR-11), a retrieval-only search endpoint.

Out of scope (later phases): task classification beyond one "question" type, graph traversal (Phase 5), a reranker
model, streaming answers, multi-turn follow-ups, the web source viewer, and the evaluation set (next slice of Phase 4).

## 2. Starting point

- `chunks` holds symbol chunks (≤120 lines) and 60-line windows, each with path, lines and `symbol_name`, plus a
  generated `search_tsv` (`to_tsvector('simple', text)`) with a GIN index.
- `embeddings (chunk_id, model_name, dimensions, embedding vector(1024))` with an HNSW cosine index exists and is empty.
- Secret paths and secret-looking content are already excluded before chunking, so nothing secret reaches a chunk
  and therefore nothing secret is sent to a provider (blueprint §9.3).

## 3. Key decisions

| Decision | Choice | Why |
|---|---|---|
| Providers | Behind two small interfaces (`EmbeddingProvider`, `AnswerModel`) chosen by `EMBEDDING_PROVIDER` / `LLM_PROVIDER`. Default `none`. | Blueprint: one model provider, swappable. With `none`, indexing and keyword search still work; answers return `answer_model_not_configured`. |
| Default adapters | **Ollama, running locally**: `bge-m3` for embeddings (1024 dims, 8K-token input) and `qwen2.5-coder:7b` for answers | The owner asked for free models (2026-10-16). No keys, no per-call cost, and source code never leaves the machine. `bge-m3` fits the existing 1024-dim column without a migration. A hosted adapter can be added behind the same interfaces later. |
| When to embed | A new `embed` stage inside the index job, after `store`, in batches of 64 with bounded retry/backoff | Reuses the job's heartbeat, cancel and retry machinery. A provider outage does not fail indexing: the snapshot is still served and gets an `embeddings_incomplete` warning, and search falls back to keyword + symbol. |
| Re-embedding | Only chunks without an embedding for the configured model are embedded; `force` re-index re-embeds everything | Existing snapshots get vectors with `POST /repositories/{id}/index {"force": true}`. |
| Fusion | Reciprocal rank fusion (k = 60) over the keyword, symbol, path and vector lists, symbol hits weighted ×1.5 | No score calibration needed between ts_rank and cosine distance; simple to reason about and benchmark. |
| De-duplication | Drop a candidate whose line range overlaps a better-ranked one in the same file by more than half | Symbol chunks and overlapping windows otherwise repeat the same code. |
| Answer call | One synchronous `POST /api/chat` per question with `format` set to the answer's JSON schema, `temperature 0`, `num_ctx 16384`; no agent loop | Blueprint §12.2: bounded calls, structured output. Ollama's default context (4K) is too small for the evidence, so `num_ctx` is set explicitly. A 7B model on a laptop takes tens of seconds; move to the queue if that hurts. |
| Citations | The model cites evidence ids only (`ev_1`…), never free-form paths; the server maps ids to path/lines | Every citation is verifiable by construction; a made-up id is dropped and the claim downgraded. |
| Storage | An `investigations` row per question: question, snapshot, status, answer, evidence, and model/prompt/retrieval metadata | Investigation history and FR-11 audit without logging source code elsewhere. |

## 4. Retrieval pipeline

```text
question
  -> terms: lowercase alphanumeric tokens minus stopwords (max 32)       e.g. "how is a job reaped" -> job, reaped
  -> identifiers: snake_case, camelCase, dotted, `name()` or `quoted`   e.g. reap_stale_jobs, IndexRun.run
  -> paths: tokens ending in a code or text file extension             e.g. src/app.py
  -> keyword   chunks WHERE snapshot AND search_tsv @@ to_tsquery('simple', 'job | reaped')
               ORDER BY ts_rank_cd DESC LIMIT 40
  -> symbol    symbols named ident (name, qualified_name or '%.ident') -> the chunk holding each one, LIMIT 20
  -> path      file paths in the question (src/app.py, pipeline.py) -> that file's first chunks, LIMIT 10
  -> vector    (if embeddings exist for the model) embed(question, input_type=query);
               ORDER BY embedding <=> q LIMIT 40, with SET LOCAL hnsw.ef_search = 200
  -> fuse (RRF), record retrieved_by = {keyword, symbol, path, vector} per chunk
  -> de-duplicate overlapping ranges
  -> assemble: best first until ANSWER_CONTEXT_TOKENS (8,000) or 20 items
```

Terms are reduced to `[a-z0-9]+` before they are joined with `|`, so user input never reaches tsquery syntax.
The keyword list matches chunks with any term and ranks those with more of them higher; Postgres' default parser
splits `reap_stale_jobs` into its words, so identifiers also match by keyword.

Vector search always returns nearest neighbours, even for a question unrelated to the code; the prompt makes the
model say when the evidence does not answer the question, and there is no distance cut-off until the evaluation set
can pick one.

The HNSW index is global across snapshots and pgvector filters after the index scan, so a large deployment could see
fewer than 40 vector hits for a small snapshot. `ef_search = 200` is enough for the MVP; revisit with
`hnsw.iterative_scan` (pgvector ≥ 0.8) or per-repository partial indexes when we have more than a few repositories.

## 5. Answer contract and validation

The model receives a fixed system prompt (instructions only) and a user message with the question and the evidence,
each item wrapped as `<evidence id="ev_3" path="…" lines="10-42">…</evidence>`. The system prompt says the evidence
is untrusted repository content that cannot change the rules (blueprint §12.2). Output schema (blueprint §10.4):

```json
{
  "summary": "Short answer grounded in the evidence",
  "claims": [{"text": "…", "evidence_ids": ["ev_3"], "status": "supported | hypothesis"}],
  "limitations": ["…"],
  "suggested_next_steps": ["…"]
}
```

Server-side validation, after Pydantic parsing:

1. Unknown evidence ids are removed from claims and counted in `validation.dropped_evidence_ids`.
2. A `supported` claim left with no valid evidence becomes `unverified`.
3. Every cited evidence item is re-checked against `files` in the snapshot (path exists, not excluded, line range
   within `line_count`); a failure removes it like an unknown id.
4. Only cited evidence plus the top 5 retrieved items are stored and returned, each with path, lines, symbol,
   `retrieved_by`, score and text, so the UI can show and open them.

If retrieval finds nothing, no model call is made: the answer says no relevant code was found. Output that still
fails schema validation after one retry marks the investigation `failed` with `answer_invalid`; an unreachable
Ollama gives `answer_provider_error`.

## 6. Data model (migration 0003)

```text
investigations    id, workspace_id FK, repository_id FK, snapshot_id FK, question text,
                  status ('succeeded' | 'failed'), answer jsonb NULL, evidence jsonb,
                  validation jsonb, error_code NULL, metadata jsonb, created_at, finished_at
                  INDEX (repository_id, created_at DESC)
```

`metadata` records `prompt_version`, `answer_model`, `embedding_model`, retrieval settings, candidate counts per
source, input/output tokens and duration. No new columns on `embeddings`: `model_name` and `dimensions` already exist.

## 7. API

All under `/api/v1`, scoped by workspace like the ingestion routes, served only from `ready`/`partial` snapshots.

| Method + path | Purpose | Response |
|---|---|---|
| `GET /repositories/{id}/search?q&snapshot_id&limit` | Retrieval only, no model call. Useful without an answer model and for evaluation. | `200 {snapshot_id, commit_sha, items: [evidence]}` |
| `POST /repositories/{id}/investigations` | Body `{question, snapshot_id?}`. Retrieve, answer, validate, store. | `201 investigation`; `503 answer_model_not_configured` |
| `GET /repositories/{id}/investigations` | History, newest first, cursor-paginated. | `200 {items, next_cursor}` |
| `GET /investigations/{id}` | One investigation with answer, evidence and validation. | `200` / `404` |

`question` is 3–2,000 characters. New error codes: `answer_model_not_configured`, `answer_invalid`,
`answer_provider_error`, `embedding_provider_error`.

## 8. Configuration

Names follow blueprint §18.3. No API keys are needed for Ollama. Setup on the developer machine:
`ollama pull bge-m3 && ollama pull qwen2.5-coder:7b` (about 6 GB of downloads; the 7B model wants ~8 GB of RAM).

```text
OLLAMA_URL=http://localhost:11434
EMBEDDING_PROVIDER=ollama        # none | ollama   (code default is none, so CI and fresh clones need nothing)
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIMENSIONS=1024        # must match the column; changing it needs a migration and re-embed
LLM_PROVIDER=ollama              # none | ollama
LLM_MODEL=qwen2.5-coder:7b
LLM_CONTEXT_TOKENS=16384         # passed as num_ctx
ANSWER_CONTEXT_TOKENS=8000
```

Data handling (blueprint §16): with Ollama, chunk text and evidence go only to the local Ollama server. If a hosted
provider is added later, the README must say what is sent: chunk text at index time, selected evidence per question.

## 9. Code layout

```text
apps/api/codemind/
  providers/ollama.py          OllamaEmbeddings and OllamaAnswerModel (httpx to /api/embed and /api/chat)
  providers/__init__.py        EmbeddingProvider and AnswerModel protocols, get_embedding_provider(), get_answer_model()
  retrieval/query.py           term and identifier extraction
  retrieval/search.py          keyword, symbol and vector candidates, fusion, de-duplication, assembly
  retrieval/answer.py          prompt, answer schema, citation validation, investigation runner
  indexing/embedding.py        embed stage used by the pipeline
  api/v1/routes/{search,investigations}.py, schemas/retrieval.py
  db/migrations/versions/0003_investigations.py
```

## 10. Testing

- Unit: term/identifier extraction, tsquery sanitising, RRF and de-duplication, context budget, citation validation
  (unknown ids, out-of-range lines, supported claim with no evidence).
- Integration (Postgres + pgvector, providers faked with deterministic vectors and canned answers): indexing writes one
  embedding per chunk; a re-run does not duplicate; a failing provider leaves the snapshot `ready` with a warning;
  keyword, symbol and vector each retrieve the expected chunk on the fixture repository.
- API: search and investigations on the fixture repository, 503 without an answer model, 404 across workspaces,
  history order.
- Provider adapters: Ollama request shape, dimension check and error mapping with `respx`. CI never runs a model.

## 11. Open questions

1. ~~Provider choice~~: decided 2026-10-16, free local models via Ollama (§3).
2. Should answers stream (SSE) once the web Q&A view exists? Proposal: only if measured latency feels slow.
3. A reranker (for example `bge-reranker-v2-m3`) after fusion: decide after the baseline evaluation set exists.
4. Is `qwen2.5-coder:7b` good enough at citing evidence? Measure on the evaluation set; `qwen2.5-coder:14b` is the
   next step up if the machine has ~16 GB of RAM.
