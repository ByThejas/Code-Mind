"""Read a unified diff (git diff, diff -u) into the lines it changes in the old version of each file.

The indexed snapshot is the code before the change, so changes are located with old-side line
numbers. Removed lines count as changed; an added line marks the old line it follows.
"""

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")


@dataclass
class FileChange:
    old_path: str | None  # None for a new file
    new_path: str | None  # None for a deleted file
    lines: set[int] = field(default_factory=set)  # old-side line numbers

    @property
    def path(self) -> str:
        return self.old_path or self.new_path or ""

    @property
    def status(self) -> str:
        if self.old_path is None:
            return "added"
        if self.new_path is None:
            return "deleted"
        if self.old_path != self.new_path:
            return "renamed"
        return "modified"


def _strip(raw: str) -> str | None:
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_unified_diff(text: str) -> list[FileChange]:
    changes: list[FileChange] = []
    current: FileChange | None = None
    old_line = 0
    old_left = new_left = 0  # lines still to read in the current hunk
    pending_old: str | None = None
    header_open = False  # a "diff --git" line opened a file that ---/+++ lines may refine
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            match = _GIT_HEADER.match(raw)
            current = FileChange(match.group(1), match.group(2)) if match else None
            if current is not None:
                changes.append(current)
            header_open = current is not None
            old_left = new_left = 0
            continue
        in_hunk = old_left > 0 or new_left > 0
        if raw.startswith("--- ") and not in_hunk:
            pending_old = raw[4:]
            continue
        if raw.startswith("+++ ") and pending_old is not None:
            old, new = _strip(pending_old), _strip(raw[4:])
            pending_old = None
            if current is not None and header_open:
                current.old_path, current.new_path = old, new
            else:
                current = FileChange(old, new)
                changes.append(current)
            header_open = False
            continue
        if current is None:
            continue
        if raw.startswith("deleted file mode"):
            current.new_path = None
            continue
        if raw.startswith("new file mode"):
            current.old_path = None
            continue
        if raw.startswith("rename from "):
            current.old_path = raw[len("rename from ") :]
            continue
        if raw.startswith("rename to "):
            current.new_path = raw[len("rename to ") :]
            continue
        match = _HUNK.match(raw)
        if match:
            old_line = int(match.group(1))
            old_left = int(match.group(2) or 1)
            new_left = int(match.group(4) or 1)
            continue
        if not in_hunk:
            continue
        if raw.startswith("-"):
            current.lines.add(old_line)
            old_line += 1
            old_left -= 1
        elif raw.startswith("+"):
            current.lines.add(max(old_line - 1, 1))
            new_left -= 1
        elif raw.startswith(" ") or raw == "":
            old_line += 1
            old_left -= 1
            new_left -= 1
    return changes
