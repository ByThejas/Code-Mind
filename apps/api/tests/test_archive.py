from pathlib import Path

from codemind.indexing.archive import ArchiveLimits, scan_archive
from tests.fixtures import EXPECTED_EXCLUDED, EXPECTED_INCLUDED, make_tarball


def _scan(tmp_path: Path, limits: ArchiveLimits, **kwargs: object) -> dict:
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(make_tarball(**kwargs))  # type: ignore[arg-type]
    return {f.path: f for f in scan_archive(archive, limits)}


def test_scan_applies_filters_and_strips_prefix(tmp_path: Path) -> None:
    files = _scan(tmp_path, ArchiveLimits(max_files=100, max_file_bytes=5000))

    included = {p for p, f in files.items() if f.excluded_reason is None}
    excluded = {p: f.excluded_reason for p, f in files.items() if f.excluded_reason}
    assert included == EXPECTED_INCLUDED
    assert excluded == EXPECTED_EXCLUDED
    # Traversal and absolute members never appear, under any name.
    assert not any("escape" in p or p.startswith("/") or "abs" in p for p in files)
    app = files["src/app.py"]
    assert app.text is not None and app.text.startswith("import os")
    assert app.content_hash is not None and len(app.content_hash) == 64
    assert files[".env"].text is None


def test_file_limit_marks_the_rest(tmp_path: Path) -> None:
    files = _scan(tmp_path, ArchiveLimits(max_files=2, max_file_bytes=5000))
    included = [f for f in files.values() if f.excluded_reason is None]
    assert len(included) == 2
    # Files that pass every other rule after the first two hit the limit.
    limited = {p for p, f in files.items() if f.excluded_reason == "limit_reached"}
    assert limited == {"src/broken.py", "web/index.ts", ".env.example", "leak.py", "logo.png"}
