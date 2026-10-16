"""Phase 1 spike: fetch a public GitHub repository at a pinned commit and report what would be indexed.

    python scripts/spike_fetch.py owner/repo [ref]
    python scripts/spike_fetch.py --archive path/to/repo.tar.gz

Nothing is written to the database. Uses the same fetch, filter and parse code as the worker.
"""

import argparse
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from codemind.core.config import settings
from codemind.indexing.archive import ArchiveLimits, scan_archive
from codemind.indexing.chunking import chunk_file
from codemind.parsing.treesitter import SUPPORTED_LANGUAGES, parse_symbols
from codemind.repositories.github import GitHubClient, parse_repository_url


def report(archive: Path) -> None:
    limits = ArchiveLimits(settings.max_repository_files, settings.max_file_bytes)
    excluded: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    symbols: Counter[str] = Counter()
    parse_errors = chunks = included = 0
    started = time.perf_counter()
    for entry in scan_archive(archive, limits):
        if entry.excluded_reason:
            excluded[entry.excluded_reason] += 1
            continue
        included += 1
        languages[entry.language or "unknown"] += 1
        parsed = None
        if entry.language in SUPPORTED_LANGUAGES and entry.text is not None:
            result = parse_symbols(entry.language, entry.text)
            if result.has_errors:
                parse_errors += 1
            else:
                parsed = result.symbols
                symbols[entry.language] += len(parsed)
        chunks += len(chunk_file(entry.text or "", parsed))
    elapsed = time.perf_counter() - started

    print(f"included files: {included}   chunks: {chunks}   scan+parse: {elapsed:.2f}s")
    print("excluded:", dict(excluded.most_common()) or "none")
    print("languages:", dict(languages.most_common(10)))
    print("symbols:", dict(symbols), f"  files with parse errors: {parse_errors}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repository", nargs="?")
    ap.add_argument("ref", nargs="?")
    ap.add_argument("--archive", type=Path)
    args = ap.parse_args()

    if args.archive:
        report(args.archive)
        return 0
    if not args.repository:
        ap.error("give owner/repo or --archive")

    target = parse_repository_url(args.repository)
    with GitHubClient(settings) as gh, tempfile.TemporaryDirectory() as tmp:
        meta = gh.get_repository(target.owner, target.name)
        ref = args.ref or target.ref or meta.default_branch
        sha = gh.resolve_ref(target.owner, target.name, ref)
        print(
            f"{meta.owner}/{meta.name} {ref} -> {sha}  (private={meta.private}, ~{meta.size_kb} KB)"
        )
        started = time.perf_counter()
        size = gh.download_tarball(target.owner, target.name, sha, Path(tmp) / "repo.tar.gz")
        print(f"tarball: {size / 1024:.0f} KB in {time.perf_counter() - started:.2f}s")
        report(Path(tmp) / "repo.tar.gz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
