"""Check a patch given as find-and-replace edits, and turn it into a unified diff.

Edits are applied to the snapshot's stored file text in memory, never to a checkout, so nothing here
touches the filesystem. Each edit names a file, an exact piece of its current text, and what to put
there instead. Exact text is easier for a small model to get right than diff hunks with line
numbers, and it either matches once or the edit is rejected, so a patch can never land somewhere
other than where it says.
"""

import difflib
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from codemind.indexing.filters import content_exclusion, path_exclusion

MAX_PATH_LENGTH = 1000


@dataclass(frozen=True)
class Edit:
    path: str
    old_text: str  # "" creates `path`, which must not exist yet
    new_text: str


@dataclass(frozen=True)
class SnapshotFile:
    """What the checker needs to know about a path that exists in the snapshot."""

    text: str | None  # None when the file was excluded from indexing and its text is not stored
    excluded_reason: str | None = None


@dataclass(frozen=True)
class PatchLimits:
    max_edits: int = 20
    max_files: int = 10
    max_changed_lines: int = 400
    max_file_bytes: int = 1_000_000


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    edit_index: int | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "edit_index": self.edit_index,
            "path": self.path,
        }


@dataclass(frozen=True)
class FileDiff:
    path: str
    status: Literal["added", "modified"]
    additions: int
    deletions: int
    diff: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "status": self.status,
            "additions": self.additions,
            "deletions": self.deletions,
        }


@dataclass
class PatchCheck:
    files: list[FileDiff] = field(default_factory=list)
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.files)

    @property
    def diff(self) -> str:
        return "".join(f.diff for f in self.files)

    @property
    def diff_sha256(self) -> str:
        return hashlib.sha256(self.diff.encode()).hexdigest()


class PathError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_path(raw: str) -> str:
    """A repository-relative POSIX path, or PathError. Nothing that could leave the repository."""
    path = raw.removeprefix("./")
    if not path or len(path) > MAX_PATH_LENGTH:
        raise PathError("invalid_path", "The path is empty or too long.")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in path):
        raise PathError("invalid_path", "The path contains control characters.")
    if "\\" in path:
        raise PathError("invalid_path", "Use forward slashes in paths.")
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        raise PathError("invalid_path", "The path must be relative to the repository root.")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PathError("invalid_path", "The path has empty, '.' or '..' segments.")
    if ".git" in parts:
        raise PathError("sensitive_path", "Files under .git cannot be changed.")
    reason = path_exclusion(path)
    if reason == "secret_path":
        raise PathError("sensitive_path", "Files that usually hold secrets cannot be changed.")
    if reason is not None:
        raise PathError(
            "excluded_path", f"This path is not indexed ({reason}), so it is not editable."
        )
    return path


def check_edits(
    edits: list[Edit],
    files: Mapping[str, SnapshotFile],
    limits: PatchLimits | None = None,
    allowed_paths: set[str] | None = None,
) -> PatchCheck:
    """Apply `edits` in order to the snapshot `files` and report the diff, errors and warnings.

    `files` must hold every existing path the edits name (after normalize_path). When
    `allowed_paths` is given, existing files outside it cannot be edited: a model may only change
    code it was shown.
    """
    limits = limits or PatchLimits()
    check = PatchCheck()
    if not edits:
        check.errors.append(Issue("no_edits", "The patch has no edits."))
        return check
    if len(edits) > limits.max_edits:
        check.errors.append(
            Issue("too_many_edits", f"A patch can have at most {limits.max_edits} edits.")
        )
        return check

    original: dict[str, str | None] = {}  # None: the patch creates this file
    working: dict[str, str] = {}
    for index, edit in enumerate(edits):
        try:
            path = normalize_path(edit.path)
        except PathError as exc:
            check.errors.append(Issue(exc.code, exc.message, index, edit.path))
            continue

        def error(code: str, message: str, path: str = path, index: int = index) -> None:
            check.errors.append(Issue(code, message, index, path))

        if path not in working:
            existing = files.get(path)
            if existing is None:
                if edit.old_text:
                    error(
                        "file_not_found",
                        "The file is not in the snapshot. To create it, leave old_text empty.",
                    )
                    continue
                original[path], working[path] = None, ""
            elif existing.text is None:
                error(
                    "file_excluded",
                    f"The file was not indexed ({existing.excluded_reason}), so it is not editable.",
                )
                continue
            elif allowed_paths is not None and path not in allowed_paths:
                error("path_not_in_evidence", "The model may only edit files it was shown.")
                continue
            else:
                original[path] = working[path] = existing.text

        current = working[path]
        if edit.old_text == edit.new_text:
            error("no_change", "old_text and new_text are the same.")
            continue
        if not edit.old_text:
            if original[path] is None and current == "":
                working[path] = edit.new_text
            else:
                error("old_text_required", "The file exists, so old_text must say what to replace.")
            continue
        count = current.count(edit.old_text)
        if count == 1:
            working[path] = current.replace(edit.old_text, edit.new_text, 1)
            continue
        if count > 1:
            error(
                "old_text_ambiguous",
                f"old_text appears {count} times. Include more surrounding lines.",
            )
            continue
        span = _loose_span(current, edit.old_text)
        if span is None:
            error("old_text_not_found", "old_text does not appear in the file.")
            continue
        if span == "ambiguous":
            error(
                "old_text_ambiguous",
                "old_text matches several places when trailing spaces are ignored.",
            )
            continue
        start, end, keep_newline = span
        replacement = edit.new_text
        if keep_newline and not replacement.endswith("\n"):
            replacement += "\n"
        working[path] = current[:start] + replacement + current[end:]
        check.warnings.append(
            Issue(
                "matched_ignoring_whitespace",
                "old_text matched only after ignoring trailing spaces and line endings.",
                index,
                path,
            )
        )

    changed = [p for p in working if working[p] != (original[p] or "") or original[p] is None]
    if len(changed) > limits.max_files:
        check.errors.append(
            Issue("too_many_files", f"A patch can change at most {limits.max_files} files.")
        )
    total = 0
    for path in changed:
        new = working[path]
        if len(new.encode()) > limits.max_file_bytes:
            check.errors.append(
                Issue("file_too_large", "The changed file would be too large.", path=path)
            )
            continue
        if content_exclusion(new.encode()) == "secret_content":
            check.errors.append(
                Issue("secret_content", "The change adds what looks like a credential.", path=path)
            )
            continue
        file_diff = _diff(path, original[path], new)
        total += file_diff.additions + file_diff.deletions
        check.files.append(file_diff)
    if total > limits.max_changed_lines:
        check.errors.append(
            Issue(
                "too_many_changed_lines",
                f"The patch changes {total} lines; the limit is {limits.max_changed_lines}.",
            )
        )
    if not check.errors and not check.files:
        check.errors.append(Issue("no_changes", "The edits leave every file unchanged."))
    return check


def _loose_span(text: str, old: str) -> tuple[int, int, bool] | Literal["ambiguous"] | None:
    """Find `old` as whole lines, ignoring trailing spaces and CRLF/LF differences.

    Returns the (start, end) character span of the matching lines and whether that span ends with
    a newline, "ambiguous" for several matches, or None.
    """
    wanted = [line.rstrip() for line in old.strip("\r\n").splitlines()]
    if not wanted or not any(wanted):
        return None
    lines = text.splitlines(keepends=True)
    stripped = [line.rstrip() for line in lines]
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    found = [
        i for i in range(len(lines) - len(wanted) + 1) if stripped[i : i + len(wanted)] == wanted
    ]
    if not found:
        return None
    if len(found) > 1:
        return "ambiguous"
    first = found[0]
    last = first + len(wanted) - 1
    return offsets[first], offsets[last + 1], lines[last].endswith(("\n", "\r"))


def _diff(path: str, old: str | None, new: str) -> FileDiff:
    """A git-style diff for one file that `git apply` accepts."""
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    body = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{path}" if old is not None else "/dev/null",
        tofile=f"b/{path}",
        n=3,
    )
    out = [f"diff --git a/{path} b/{path}\n"]
    if old is None:
        out.append("new file mode 100644\n")
    additions = deletions = 0
    for i, line in enumerate(body):
        if i >= 2:  # after the ---/+++ header lines
            if line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1
        if not line.endswith("\n"):
            line += "\n\\ No newline at end of file\n"
        out.append(line)
    status: Literal["added", "modified"] = "added" if old is None else "modified"
    return FileDiff(path, status, additions, deletions, "".join(out))
