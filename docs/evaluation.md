# Retrieval Evaluation

Status: first slice · 2026-10-20 · Source: CODEMIND blueprint v1.0 §17 and Phase 4 ("create baseline evaluation cases and measure retrieval/citation quality")

## 1. What it measures

For each question in a fixed dataset, run retrieval the way an investigation does and check whether the evidence
contains the files (and symbols) a good answer needs. No answer model is called, so results are deterministic and
cost nothing. Each dataset runs under four configurations, so each technique is compared with the simpler one before
it on the same questions:

| Config | Retrieval |
|---|---|
| `keyword` | Full-text, symbol-name and path search, fused with RRF |
| `keyword_graph` | + graph expansion (what investigations do without Ollama) |
| `hybrid` | `keyword` + vector search (needs embeddings) |
| `hybrid_graph` | + graph expansion (what investigations do with Ollama) |

A vector configuration is reported as skipped, not run, when there is no embedding provider or the snapshot has no
embeddings for it, since it would only repeat `keyword`.

Metrics per case, averaged over a configuration and per category:

| Metric | Meaning |
|---|---|
| `recall` | Share of the expected files that appear in the evidence (recall@k). |
| `precision` | Share of evidence items that come from an expected file (precision@k). |
| `mrr` | 1 / rank of the first evidence item from an expected file (0 if none). |
| `hit_rate` | Share of cases with at least one expected file. |
| `full_recall_rate` | Share of cases with every expected file. |
| `symbol_recall` | Share of expected symbols named by an evidence chunk, where a case lists them. |
| `latency_ms_p50` | Median retrieval time. |

Precision is low by design at large k: one expected file and 20 evidence items gives at most 0.05 per correct item.
Compare it between configurations, not against 1.0.

## 2. Datasets

Versioned JSON files in `apps/api/evaluation/datasets/`. Each case has an `id`, a `category` (`symbol_lookup`,
`explanation`, `flow`, `config`), the `question`, `expected_files`, and optional `expected_symbols`. A dataset names
the repository and the commit its expectations were written against; when code moves, add a new version rather than
editing an old one, so past results stay comparable.

| Dataset | Cases | Repository | Runs |
|---|---|---|---|
| `shop-fixture.v1.json` | 10 | The shop fixture in `tests/graph_fixtures.py` | In CI (`tests/test_evaluation.py`), with the deterministic test embedder |
| `code-mind.v1.json` | 16 | ByThejas/Code-Mind at `dfdaa75` | By hand, with `scripts/evaluate_retrieval.py` |

## 3. Running it on a real repository

```bash
cd apps/api
# Index the commit the dataset was written for (API and worker running):
curl -X POST localhost:8000/api/v1/repositories -H 'content-type: application/json' \
  -d '{"url": "ByThejas/Code-Mind", "ref": "dfdaa75a70de5ce41cba893c122d5d32d2c5322b"}'
# Then:
python scripts/evaluate_retrieval.py evaluation/datasets/code-mind.v1.json --verbose
python scripts/evaluate_retrieval.py evaluation/datasets/code-mind.v1.json --k 6 --json report.json
```

`--k` defaults to 20, the number of evidence items an investigation sends to the answer model. With
`EMBEDDING_PROVIDER=ollama` (bge-m3) in `.env` and an index built with it, the vector configurations run too.
`--verbose` prints which expected files each case missed; `--json` writes every case's result with the commit,
k and embedding model, so runs can be compared later.

## 4. Baselines

Shop fixture, k = 6, test embedder (asserted in CI):

| Config | Recall | Precision | MRR | Full recall |
|---|---|---|---|---|
| keyword | 0.82 | 0.47 | 0.83 | 0.70 |
| keyword_graph | 0.90 | 0.41 | 0.83 | 0.90 |
| hybrid | 0.85 | 0.33 | 0.85 | 0.70 |
| hybrid_graph | 0.93 | 0.42 | 0.85 | 0.90 |

Code-Mind at `dfdaa75`, no embeddings (16 cases):

| Config | k | Recall | Precision | MRR | Full recall |
|---|---|---|---|---|---|
| keyword | 20 | 0.88 | 0.15 | 0.52 | 0.88 |
| keyword_graph | 20 | 0.88 | 0.19 | 0.52 | 0.88 |
| keyword | 6 | 0.75 | 0.24 | 0.51 | 0.69 |
| keyword_graph | 6 | 0.75 | 0.26 | 0.51 | 0.69 |

What the Code-Mind run shows:

- Symbol lookups always find the file (recall 1.0, MRR 0.81). Questions in plain words are weaker: explanation
  recall is 0.60 at k = 6.
- Two cases miss entirely even at k = 20: "How are files that look like secrets kept out of the index?" (the code
  says `exclusion` and `SECRET_`, the question says "secrets" and "kept out") and "What happens when a repository is
  imported through POST /repositories?". Wording mismatches like these are what vector search is for; run with bge-m3
  to measure how much it closes the gap.
- On this repository graph expansion raises precision but not recall without embeddings: the start of the flow is
  either found by keywords, and its callees with it, or not at all.

Vector numbers on Code-Mind need Ollama and are not recorded yet. Add them here when you run them, with the model
name and k.

## 5. Determinism

Building this surfaced that retrieval broke ties on random chunk ids, so the same code indexed twice could rank
differently (the shop fixture's keyword recall moved between 0.82 and 0.85 from run to run). Keyword, symbol, vector
and graph candidates now break ties on file path and line, so a dataset gives the same numbers on every index of the
same commit.

## 6. Not yet

Citation validity and support, answer correctness and abstention need the answer model and a judging method
(blueprint §17: deterministic checks first, LLM-as-judge only as a supplementary signal). Impact and patch tasks
get their own cases when those features are evaluated (Phase 8). Results are not yet stored in the database or
exposed at `GET /api/v1/evaluations/runs`.
