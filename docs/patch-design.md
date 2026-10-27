# Patch Proposals — Design (Phase 7, part 1)

Status: implemented in apps/api (first slice) · 2026-10-26 · Source: CODEMIND blueprint v1.0 §3.3, §13, §14, §15 (Phase 7)

## 1. Goal and scope

Turn "how should this change?" into a reviewable patch without ever touching a repository. Blueprint exit gate for
Phase 7: no patch is applied without approval; test status reflects actual execution.

This slice covers the patch format, validation against the indexed snapshot, the approval state machine, export of
the approved diff, and an audit log. Running tests on a patched copy is in docs/sandbox-design.md. Planned next: a patch review view
in the web app.

## 2. Patch format: exact edits, not hunks

The model returns edits, each `{path, old_text, new_text}`:

- `old_text` is copied from the code the model was shown and must appear **exactly once** in the file. Two matches
  is `old_text_ambiguous`, none is `old_text_not_found`.
- An empty `old_text` creates a new file, which must not exist yet.
- Edits apply in order, so a later edit sees the result of earlier ones.

Why not unified diffs from the model: small local models (qwen2.5-coder:7b) get hunk line numbers and context counts
wrong often, and a fuzzy patcher could then put a change in the wrong place. With exact text, an edit either lands
where it says or is rejected. The server then writes the unified diff itself with `difflib`, in git format, so
`git apply` accepts it.

One tolerance: when exact text is not found, a match ignoring trailing spaces and CRLF/LF differences is accepted if
it is unique, with a `matched_ignoring_whitespace` warning. Indentation is never ignored, since it carries meaning in
Python.

## 3. Validation (`codemind/patches/edits.py`)

All checks run in memory on the snapshot's stored file text. Nothing is written to disk.

| Check | Error code |
|---|---|
| Path is relative, forward slashes, no `..`, `.` or empty segments, no control characters, no drive letter | `invalid_path` |
| Path is not under `.git` and not a secret file (`.env`, keys, `secrets.*`, the indexer's rules) | `sensitive_path` |
| Path is not ignored or generated (`node_modules`, lock files, …) | `excluded_path` |
| File exists for a replace, does not exist for a create, and was indexed | `file_not_found`, `old_text_required`, `file_excluded` |
| Model edits touch only files that were in its evidence (new files are allowed) | `path_not_in_evidence` |
| Result does not add something that looks like a credential | `secret_content` |
| At most 20 edits, 10 files, 400 changed lines, `MAX_FILE_BYTES` per file | `too_many_edits`, `too_many_files`, `too_many_changed_lines`, `file_too_large` |

Every error names the edit index and path, so a reviewer (or a later retry) can see which edit failed and why.
Limits are settings: `PATCH_MAX_EDITS`, `PATCH_MAX_FILES`, `PATCH_MAX_CHANGED_LINES`.

## 4. Lifecycle

```
            ┌──────────► invalid ──────────┐
create ─────┤                              ├──► rejected
            ├──────────► validated ──┬─────┘
            │                        └──► approved ──► exported
            └──────────► failed  (model error or no evidence)
```

- `validated → approved` needs `diff_sha256` equal to the stored diff's hash, so what was approved is exactly what
  was reviewed (`diff_mismatch` otherwise).
- `approved → rejected` is allowed (change of mind); after export it is not.
- `export` only works on `approved`/`exported` patches and returns the diff and a file name. It never applies the
  diff anywhere; that stays the developer's step (`git apply` on a checkout of the same commit).
- Invalid and failed patches are stored, not discarded, so failures are visible and measurable.

Test results are kept as separate test runs (docs/sandbox-design.md) rather than as patch states, so a patch can
be tested several times and each result names the exact diff it tested.

## 5. Generation

`POST /investigations/{id}/patches` with only an `instruction` retrieves evidence for the instruction plus the
investigation's question (same hybrid retrieval as answers, 6,000-token budget, at most 12 items) and sends the
model the instruction, the original question and answer summary, and the evidence. The prompt says the evidence is
untrusted data. Output is constrained to the `PatchDraft` JSON schema through Ollama's `format` option, with one
retry on schema failure. Prompt version `patch-v1`, model, tokens, evidence paths and duration are stored in the
patch's `metadata`.

With `edits` in the body, no model is called and the edits are only checked: useful for checking a hand-written
change, and how the tests drive the API.

## 6. API

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/investigations/{id}/patches` | `{instruction, edits?}` → 201 with the patch in any outcome; 503 when no model and no edits |
| GET | `/api/v1/investigations/{id}/patches` | Newest first |
| GET | `/api/v1/patches/{id}` | Diff, files with line counts, validation errors and warnings |
| POST | `/api/v1/patches/{id}/approve` | `{diff_sha256}` |
| POST | `/api/v1/patches/{id}/reject` | `{reason?}` |
| POST | `/api/v1/patches/{id}/export` | `{filename, diff, diff_sha256, commit_sha}` |

Everything is scoped to the caller's workspace; a patch in another workspace is a 404.

## 7. Data

`patches`: instruction, source (`model`/`manual`), status, summary, limitations, edits as given, per-file
additions/deletions, diff, diff SHA-256, validation, error, metadata, decision time and actor, export time. Cascades
with its repository, snapshot and investigation.

`audit_events`: actor, action (`patch.created`, `patch.approved`, `patch.rejected`, `patch.exported`), resource,
details (the approved diff hash, a rejection reason) and time. Audit rows are kept when the patch is deleted with
its repository. Until authentication lands the actor is `local-user`.
