# Impact Analysis — Design (Phase 6)

Status: implemented in apps/api (first slice) · 2026-10-20 · Source: CODEMIND blueprint v1.0 §11.2, §12.1, §15 (Phase 6)

## 1. Goal and scope

Before a refactor, answer "what might this change break, and which tests should I run?" with evidence for every
answer and a plain statement of what the analysis could not see. Exit gate (blueprint Phase 6): impact results are
reproducible and uncertainty is visible.

In scope: one endpoint that takes symbols, files, line ranges and/or a unified diff; resolution of the changed
symbols; a backwards walk over the code graph (docs/graph-design.md); related tests by graph links, file names and
symbol names; ranking; limitations. Out of scope for now: coverage data, `tested_by` edges stored at index time,
saving analyses, an impact view in the web app, and evaluation against real historical changes.

## 2. API

`POST /api/v1/repositories/{id}/impact-analysis` on the newest servable snapshot (or `snapshot_id`).

```json
{
  "targets": [
    {"symbol_id": "…"},
    {"path": "shop/billing.py"},
    {"path": "shop/services/orders.py", "start_line": 6, "end_line": 10}
  ],
  "diff": "diff --git a/shop/billing.py b/shop/billing.py\n…",
  "depth": 3,
  "min_confidence": 0.0,
  "max_results": 50
}
```

Give targets, a diff, or both. The response holds:

| Field | Meaning |
|---|---|
| `changed` | Each changed file: status, changed line ranges, the symbols those lines fall in, and whether module-level code changed. |
| `impacted` | Symbols and files that depend on the change, ranked. Each has `distance`, `confidence`, `score`, the edge `via` which it depends on the next step, that edge's `source_line`, and `path_to_change` (node ids down to the changed code). |
| `files` | The impacted items grouped by file, best score first: the review list. |
| `tests` | Related tests, each with a `reason` and `detail` (§4). |
| `unresolved` | Paths not in the snapshot (`not_indexed`), files the diff adds (`new_file`), unknown symbol ids. |
| `limitations`, `truncated`, `explored` | What the analysis could not see (§5). |
| `settings`, `snapshot_id`, `commit_sha`, `graph` | Everything needed to reproduce the result. |

A snapshot indexed before the graph existed returns 409 `graph_not_built`.

## 3. Algorithm

1. **Resolve the change.** The snapshot is the code before the change, so diff lines are read on the old side:
   removed lines count as changed, and an added line marks the old line it follows. Each changed line maps to the
   innermost symbol that holds it; lines outside every symbol make the file itself a changed node (module-level
   code). A whole-file target, a deleted file and a renamed file change the file and every symbol in it, since
   importers name the old path. Added files have nothing depending on them yet and go to `unresolved`.
2. **Walk dependants.** Breadth-first from the changed nodes over incoming edges: `calls`, `extends` and
   `implements` into symbols, `imports` into files, up to `depth` hops, skipping edges below `min_confidence`.
   Each node is visited once (cycle protection) and keeps its most confident path; the walk stops at 5,000 nodes.
3. **Rank.** `confidence` is the product of edge confidences along the path. `score = confidence × 0.7^(distance−1)`,
   so a direct static caller scores 1.0 and every extra hop or guess lowers it. Ties break by distance, path and line,
   which keeps the order stable.

The result depends only on the snapshot, the request and these constants, so the same request returns the same
answer (tested).

## 4. Related tests

A file is a test file when its name looks like one (`test_*.py`, `*_test.py`, `conftest.py`, `*.test.ts`,
`*.spec.tsx`, …) or it sits under `tests/`, `test/`, `__tests__/`, `spec/` or `specs/`. Tests reached by the walk are
listed in `tests`, not `impacted`. Reasons, strongest first:

| Reason | How it is found | Score |
|---|---|---|
| `calls_changed_code` | The walk reached test code through calls or inheritance. | Walk score |
| `imports_changed_code` | The walk reached a test file through imports. | Walk score |
| `name_match` | A test file named after a changed file (`tests/test_billing.py` for `shop/billing.py`). | 0.5 |
| `mentions_symbol` | A test file mentions a changed symbol's name (4+ characters) as a word. | 0.35 |

Each test file appears once, under its strongest reason. Up to 2,000 test files are scanned for mentions.

## 5. Uncertainty

The response is marked `advisory: true`, and `limitations` always ends with the blueprint's warning that
reflection, dependency injection, generated code, runtime configuration, dynamic imports and external systems can
hide dependencies. Before that it adds, when they apply: changed files in languages the graph does not cover;
nothing depending on the change (unused, an entry point, or reached dynamically); links inferred from names; nodes
left beyond `depth`; a truncated result; imports the graph could not follow in this snapshot; and no related tests.

## 6. Known limitations

- Everything in docs/graph-design.md §8 applies: what the graph misses, impact analysis misses.
- Changing a class does not mark callers of its methods; target the methods too.
- A changed signature and a changed comment look the same: any changed line in a function counts.
- Name and mention matching can list unrelated tests that share a name.
- Not yet evaluated on known historical changes (blueprint Phase 6, second bullet); that needs a fixed dataset.
