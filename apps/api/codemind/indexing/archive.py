"""Read files straight out of a GitHub tarball without extracting anything to disk.

Nothing in the archive is ever written out or executed, so path traversal, symlinks and device
files cannot escape a workspace: they are simply recorded as excluded.
"""

import hashlib
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from codemind.indexing.filters import content_exclusion, detect_language, path_exclusion


@dataclass
class ScannedFile:
    path: str
    size_bytes: int
    language: str | None
    excluded_reason: str | None = None
    text: str | None = None
    content_hash: str | None = None

    @property
    def line_count(self) -> int:
        if self.text is None:
            return 0
        return self.text.count("\n") + (0 if self.text.endswith("\n") or not self.text else 1)


@dataclass
class ArchiveLimits:
    max_files: int
    max_file_bytes: int

    @property
    def max_recorded(self) -> int:
        """Entries recorded at all, so a hostile archive cannot flood the database."""
        return self.max_files + 10_000


def _normalise(member_name: str) -> str | None:
    """Strip GitHub's `owner-repo-sha/` prefix; reject anything that is not a clean relative path."""
    pure = PurePosixPath(member_name)
    if pure.is_absolute():
        return None
    parts = pure.parts[1:]
    if not parts or any(part in ("..", "") for part in parts) or "\\" in member_name:
        return None
    return "/".join(parts)


def _file_entries(tar: tarfile.TarFile) -> Iterator[tuple[tarfile.TarInfo, str]]:
    for member in tar:
        if member.isdir() or member.type in (tarfile.XGLTYPE, tarfile.XHDTYPE):
            continue
        path = _normalise(member.name)
        if path is not None:
            yield member, path


def count_entries(archive_path: Path) -> int:
    """Number of entries scan_archive would consider, for progress reporting."""
    with tarfile.open(archive_path, mode="r|gz") as tar:
        return sum(1 for _ in _file_entries(tar))


def scan_archive(archive_path: Path, limits: ArchiveLimits) -> Iterator[ScannedFile]:
    """Yield one ScannedFile per file entry in the archive, with text for those that pass filters."""
    included = 0
    seen = 0
    with tarfile.open(archive_path, mode="r|gz") as tar:
        for member, path in _file_entries(tar):
            seen += 1
            if seen > limits.max_recorded:
                return
            language = detect_language(path)
            entry = ScannedFile(path=path, size_bytes=member.size, language=language)

            if not member.isfile():
                entry.excluded_reason = "unsupported"
            elif reason := path_exclusion(path):
                entry.excluded_reason = reason
            elif member.size > limits.max_file_bytes:
                entry.excluded_reason = "too_large"
            elif included >= limits.max_files:
                entry.excluded_reason = "limit_reached"
            else:
                fh = tar.extractfile(member)
                data = fh.read(limits.max_file_bytes + 1) if fh else b""
                if reason := content_exclusion(data):
                    entry.excluded_reason = reason
                else:
                    entry.text = data.decode("utf-8")
                    entry.content_hash = hashlib.sha256(data).hexdigest()
                    included += 1
            yield entry
