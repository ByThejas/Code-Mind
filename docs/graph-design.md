# Symbols and Code Graph — Design (Phase 5)

Status: implemented in apps/api (first slice) · 2026-10-19 · Source: CODEMIND blueprint v1.0 §8, §10, §11.1, §12.1, §14, §15 (Phase 5)

## 1. Goal and scope

Know how the code connects, not only what it says: which files import which, which functions call which, and which
classes extend which, each relationship with where it was found and how sure we are. Use that to answer flow
questions ("what happens when X is called?") and to give a later architecture explorer and impact analysis
(Phase 6) something to stand on. Exit gate (blueprint Phase 5): representative flow questions outperform the
baseline on the benchmark.

In scope: import, call and inheritance extraction for Python, TypeScript/TSX and JavaScript; resolution into a
per-snapshot `graph_edges` table with provenance and confidence; symbol search and detail, neighbourhood, flow trace
and module-graph endpoints; graph expansion in retrieval; relationship hints in the answer prompt; a flow benchmark.

Out of scope (later): the visual architecture explorer in the web app (waits for the glass UI PR to merge),
`tested_by`, `routes_to`, `reads_from`/`writes_to` edges and test linking (Phase 6), tsconfig path aliases, type
inference for `obj.method()` calls, and an LLM-driven multi-step flow tracer.

## 2. Data model

`graph_edges` (migration 0004), scoped to one snapshot like every other derived row (blueprint §11.1: never mix
commits), and deleted with its files when a snapshot is re-indexed:

| Column | Meaning |
|---|---|
| `relation` | `imports` (file → file), `calls`, `extends`, `implements` (symbol → symbol) |
| `source_file_id`, `source_symbol_id` | Where the reference is. `source_symbol_id` is null for module-level code. |
| `target_file_id`, `target_symbol_id` | What it points at. `target_symbol_id` is null for `imports`. |
| `line` | The line in the source file that names the target (the evidence for the edge). |
| `resolution` | `static`: the code names the target directly. `inferred`: a best guess. |
| `confidence` | 0.3 to 1.0, set by the rule that resolved it (§3). |
| `provenance` | `tree-sitter:<language>:<relation>:<rule>`, e.g. `tree-sitter:python:calls:self-method`. |

Edges are unique per (relation, source file, source symbol, target); the first line and best rule win. Indexes cover
lookups by source and target symbol and file within a snapshot. `contains` is not stored: `symbols.parent_symbol_id`
and `symbols.file_id` already hold it.

## 3. How edges are found

Parsing (`parsing/treesitter.py`) walks the same Tree-sitter tree used for symbols and records, per file, its imports
(module, relative level, bound names, module alias) and its references (calls, `new X()`, base classes, implemented
interfaces) with the innermost enclosing symbol. Nothing is resolved during parsing.

Resolution (`graph/build.py`) runs once the whole snapshot is parsed, in a new `graph` stage of the index job between
`store` and `embed`:

1. **Imports → files.** Python relative imports resolve against the importing file's package. Absolute Python
   imports match the file's canonical module name (package root = first parent without `__init__.py`), then a unique
   dotted-path suffix; several matches pick the nearest and are `inferred`. `from pkg import mod` links to `mod.py`
   when it is a submodule. JS/TS relative specifiers try the exact path, the usual extensions, `index.*`, and `.ts`
   for a `.js` specifier. Bare specifiers are libraries. Style, image and data imports are ignored.
2. **Base classes first**, so that inherited methods can be found in step 3.
3. **Calls**, by the first rule that applies:

| Rule | Example | Resolution | Confidence |
|---|---|---|---|
| `local` | a function or class defined in an enclosing scope or the same file | static | 0.95 |
| `imported-name` | `from x import f; f()`, `import { f } from './x'` | static | 0.9 |
| `module-attribute` | `orders.place_order()`, `http.post()` with `import * as http` | static | 0.9 |
| `self-method` | `self.save()` / `this.save()` on the enclosing class | static | 0.9 |
| `class-attribute` | `Store.create()` on a known class | inferred | 0.85 |
| `re-export` | a name imported from a package `__init__.py` or `index.ts` that re-exports it | inferred | 0.75 |
| `inherited-method` | `self.log()` defined on a base class (up to 3 levels) | inferred | 0.7 |
| `name-match` | a bare name defined exactly once in the repository, same language family | inferred | 0.4 |
| `method-name-match` | `repo.store_order()` on an unknown object, method name unique and not common (`get`, `save`, …) | inferred | 0.3 |

Everything else is counted, not stored: library calls and builtins (`calls_unresolved`), library imports
(`imports_external`), local imports that match no file (`imports_unresolved`), and dynamic imports (`importlib`,
`__import__`, `require(expr)`, `import(expr)`).

Files with syntax errors get no symbols today, so they have no edges either. Each file keeps at most 5,000
references.

## 4. What the snapshot reports

`snapshot.stats.graph` holds the counters above plus `edges`. Two warnings say what the graph cannot see; neither
makes a snapshot `partial`, because all code is still indexed:

- `graph_dynamic_imports`: count and up to 20 files.
- `graph_unresolved_imports`: count and up to 20 `path:line specifier` entries (path aliases such as `@/` land here).

Snapshots indexed before this change have no `stats.graph`. The graph endpoints report
`graph.available: false` with a `graph_not_built` warning (the trace endpoint returns 409), and
`POST /repositories/{id}/index {"force": true}` builds it.

## 5. API

All under `/api/v1/repositories/{id}`, on the newest servable snapshot unless `snapshot_id` is given. Every
response carries `snapshot_id` and `commit_sha`; all but symbol search also carry a `graph` block (`available`,
`stats`, `warnings`).

| Endpoint | Purpose |
|---|---|
| `GET /symbols?q=&kind=&path_prefix=&limit=` | Find symbols; exact and prefix name matches first. |
| `GET /symbols/{symbol_id}?min_confidence=` | A symbol, its parent and children, what it calls/extends (`outgoing`) and who calls/extends it (`incoming`), up to 200 each. |
| `GET /graph/neighborhood?symbol_id=\|path=&depth=1..3&direction=in\|out\|both&relations=&min_confidence=&max_nodes=` | Breadth-first graph around a symbol (calls, inheritance) or a file (imports), with cycle protection and a node cap (`truncated`). |
| `GET /graph/trace?from_symbol_id=&to_symbol_id=&max_depth=&min_confidence=` | Shortest chain of calls between two symbols, with the calling line's text at each step and `limitations`. |
| `GET /graph/modules?depth=&path_prefix=` | Imports merged by directory: the architecture view, with file counts per module and import counts per edge. |

Nodes are `symbol:<uuid>`, `file:<uuid>` or `module:<path>`; edges carry relation, resolution, confidence,
provenance, and the file and line that are their evidence, so the UI can link every edge to source.

## 6. Retrieval and answers

After fusion (docs/retrieval-design.md §4), the top 5 candidates seed a graph expansion: two hops along outgoing
`calls`/`extends`/`implements` edges and one hop back to callers, only through edges with confidence ≥ 0.6 (so
name-only guesses never pull in code). A neighbour's score is the seed's score × edge confidence × a share per hop:
0.95 for flow questions ("what happens", "how does", "calls", "trace"…), 0.7 otherwise, and 0.7 of that for callers.
Scores are raised, never added, so a neighbour never outranks the code that led to it. Neighbours are marked
`retrieved_by: graph`.

The answer prompt (version `qa-v2`) lists the edges that connect chosen evidence items, e.g.
`ev_1 create_order_endpoint calls ev_3 place_order (shop/api/routes.py line 8, static)`, and tells the model these
are hints that may be incomplete. Investigations record `retrieval.graph_relations`.

## 7. Benchmark

`tests/test_graph_benchmark.py` indexes a small shop fixture (Python service + TS client, `tests/graph_fixtures.py`)
and asks four flow questions whose answers span files. Score: recall of the needed files in the top 6 evidence
items, with the deterministic test embedder.

| Question | Baseline (Phase 4) | With graph |
|---|---|---|
| What happens when create_order_endpoint is called? | 0.67 | 1.00 |
| How does Checkout send the order to the server? | 0.33 | 0.33 |
| What does OrderService.run do? | 0.50 | 1.00 |
| What does cancel_order_endpoint do with the card? | 1.00 | 1.00 |
| **Mean** | **0.63** | **0.83** |

The test fails if any case gets worse or the mean does not improve. The Checkout miss is instructive: when the
starting code itself ranks low (every file mentions "order"), expansion has nothing good to start from. A larger,
real-repository evaluation set is Phase 8 work.

On sqlalchemy's own source (≈250k lines of Python), parsing plus graph building takes about 4 seconds and yields
13k edges: 2.7k imports, 9.1k calls (72% static), 1.3k inheritance edges, 41 dynamic imports.

## 8. Known limitations

- Calls on objects of unknown type (`repo.save()`) are only linked by a unique, uncommon method name, at 0.3.
- Callbacks, decorators, dependency injection, framework routing and dynamic dispatch are invisible.
- The enclosing symbol of a call is found by line, so two symbols on one line attribute calls to the inner one.
- tsconfig `paths`, Python `sys.path` tweaks, and monorepo package names are not read.
- Default exports resolve only when the local name matches the exported symbol's name.
