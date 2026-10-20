"""Impact analysis (blueprint §11.2): what may break when some code changes.

1. Resolve the changed symbols and files from targets and/or a unified diff.
2. Walk the snapshot's graph backwards (callers, subclasses, importers) to a set depth, with cycle
   protection, keeping the most confident path to each dependant.
3. Find related tests: test code the walk reaches, test files named after a changed file, and
   test files that mention a changed symbol.
4. Rank by confidence discounted by distance, and say what the analysis could not see.

The result is advisory. See docs/impact-design.md.
"""

import posixpath
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from codemind.core.errors import CodemindError
from codemind.db.models import File, FileContent, GraphEdge, RepositorySnapshot, Symbol
from codemind.graph import query as graph
from codemind.impact.diff import parse_unified_diff
from codemind.parsing.treesitter import SUPPORTED_LANGUAGES
from codemind.schemas.impact import (
    ChangedFile,
    ImpactFile,
    ImpactItem,
    ImpactRequest,
    ImpactResponse,
    RelatedTest,
    Unresolved,
)

DISTANCE_DECAY = 0.7  # each extra hop keeps 70% of the score
MAX_VISITS = 5000
MAX_TEST_FILES_SCANNED = 2000
NAME_MATCH_SCORE = 0.5
MENTION_SCORE = 0.35
MIN_MENTION_NAME = 4  # shorter names match too much by accident

ADVISORY = (
    "Impact analysis is advisory, not a guarantee. Reflection, dependency injection, generated "
    "code, runtime configuration, dynamic imports and external systems can create dependencies "
    "that static analysis cannot see."
)

_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_TEST_NAME = re.compile(
    r"^(?:test_.+\.py|.+_test\.py|conftest\.py|.+\.(?:test|spec)\.[cm]?[jt]sx?)$", re.IGNORECASE
)


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    return bool(_TEST_NAME.match(parts[-1])) or any(p.lower() in _TEST_DIRS for p in parts[:-1])


def _subject_stem(path: str) -> str:
    """'tests/test_billing.py' and 'src/billing.spec.ts' -> 'billing'."""
    name = posixpath.basename(path).lower()
    name = re.sub(r"\.[cm]?[jt]sx?$|\.py$", "", name)
    name = re.sub(r"\.(test|spec)$", "", name)
    name = re.sub(r"^test_|_test$", "", name)
    return name


@dataclass
class _Changed:
    file_id: uuid.UUID
    path: str
    language: str | None
    status: str
    whole: bool = False
    lines: set[int] = field(default_factory=set)
    explicit_symbols: set[uuid.UUID] = field(default_factory=set)


@dataclass
class _Reach:
    distance: int
    confidence: float
    via: GraphEdge | None  # edge from this node towards the change
    towards: str | None  # the next node towards the change
    changed: str  # the seed node it was reached from


def analyze(db: Session, snapshot: RepositorySnapshot, request: ImpactRequest) -> ImpactResponse:
    info = graph.graph_info(snapshot)
    if not info.available:
        raise CodemindError(
            "graph_not_built",
            "This snapshot has no code graph yet. Re-index it with force to build one.",
            status_code=409,
        )
    files = {
        path: (fid, language)
        for fid, path, language in db.execute(
            select(File.id, File.path, File.language).where(
                File.snapshot_id == snapshot.id, File.excluded_reason.is_(None)
            )
        ).all()
    }
    changed, unresolved = _resolve_changes(db, snapshot.id, request, files)
    changed_out, seeds = _seed(db, changed)

    reach, truncated, frontier_left = _walk(db, snapshot.id, seeds, request)
    seed_ids = set(seeds)
    reached = [n for n in reach if n not in seed_ids]
    loader = graph.NodeLoader(db, snapshot.id)
    nodes = loader.nodes(reached)
    edges = [r.via for r in reach.values() if r.via is not None]
    loader.preload_paths(edges)
    lines = graph._source_lines(db, edges)

    items: list[ImpactItem] = []
    tests: list[RelatedTest] = []
    for node_id in reached:
        node = nodes.get(node_id)
        r = reach[node_id]
        if node is None or r.via is None:
            continue
        score = round(r.confidence * DISTANCE_DECAY ** (r.distance - 1), 4)
        if is_test_path(node.path):
            relation = r.via.relation
            tests.append(
                RelatedTest(
                    path=node.path,
                    node=node,
                    reason="imports_changed_code"
                    if relation == "imports"
                    else "calls_changed_code",
                    score=score,
                    distance=r.distance,
                    detail=f"{relation} at line {r.via.line}"
                    + (f" ({r.distance} hops from the change)" if r.distance > 1 else ""),
                )
            )
            continue
        items.append(
            ImpactItem(
                node=node,
                distance=r.distance,
                confidence=round(r.confidence, 4),
                score=score,
                via=loader.edge_out(r.via),
                source_line=lines.get(r.via.id),
                changed=r.changed,
                path_to_change=_path(reach, node_id),
            )
        )
    items.sort(key=lambda i: (-i.score, i.distance, i.node.path, i.node.start_line or 0, i.node.id))
    if len(items) > request.max_results:
        items = items[: request.max_results]
        truncated = True

    by_file: dict[str, list[ImpactItem]] = defaultdict(list)
    for item in items:
        by_file[item.node.path].append(item)
    impact_files = sorted(
        (
            ImpactFile(
                path=path,
                score=max(i.score for i in group),
                distance=min(i.distance for i in group),
                items=len(group),
            )
            for path, group in by_file.items()
        ),
        key=lambda f: (-f.score, f.distance, f.path),
    )

    tests += _heuristic_tests(db, changed, changed_out, files, {t.path for t in tests})
    tests.sort(key=lambda t: (-t.score, t.path, t.node.start_line if t.node else 0))

    limitations = _limitations(
        changed, items, tests, info.warnings, truncated, frontier_left, request.depth
    )
    return ImpactResponse(
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        changed=changed_out,
        impacted=items,
        files=impact_files,
        tests=tests,
        unresolved=unresolved,
        truncated=truncated,
        explored=len(reach),
        limitations=limitations,
        settings={
            "depth": request.depth,
            "min_confidence": request.min_confidence,
            "max_results": request.max_results,
            "distance_decay": DISTANCE_DECAY,
        },
        graph=info,
    )


# --- 1. what changed -------------------------------------------------------------------------


def _resolve_changes(
    db: Session,
    snapshot_id: uuid.UUID,
    request: ImpactRequest,
    files: dict[str, tuple[uuid.UUID, str | None]],
) -> tuple[dict[str, _Changed], list[Unresolved]]:
    changed: dict[str, _Changed] = {}
    unresolved: list[Unresolved] = []

    def entry(path: str, status: str) -> _Changed | None:
        if path not in files:
            unresolved.append(Unresolved(path=path, reason="not_indexed"))
            return None
        if path not in changed:
            fid, language = files[path]
            changed[path] = _Changed(fid, path, language, status)
        return changed[path]

    for target in request.targets:
        if target.symbol_id is not None:
            row = db.execute(
                select(Symbol, File.path)
                .join(File, File.id == Symbol.file_id)
                .where(Symbol.id == target.symbol_id, Symbol.snapshot_id == snapshot_id)
            ).first()
            if row is None:
                unresolved.append(
                    Unresolved(path=None, symbol_id=target.symbol_id, reason="symbol_not_found")
                )
                continue
            symbol, path = row
            found = entry(path, "target")
            if found is not None:
                found.explicit_symbols.add(symbol.id)
                found.lines.update(range(symbol.start_line, symbol.end_line + 1))
            continue
        assert target.path is not None
        found = entry(target.path.strip("/"), "target")
        if found is None:
            continue
        if target.start_line is None and target.end_line is None:
            found.whole = True
        else:
            start = target.start_line or target.end_line or 1
            found.lines.update(range(start, (target.end_line or start) + 1))

    for change in parse_unified_diff(request.diff or ""):
        if change.status == "added":
            unresolved.append(Unresolved(path=change.new_path, reason="new_file"))
            continue
        found = entry(change.path, change.status)
        if found is None:
            continue
        if found.status == "target":
            found.status = change.status
        if change.status in ("deleted", "renamed"):
            found.whole = True  # importers name the old path
        found.lines |= change.lines
        if not change.lines and change.status == "modified":
            found.whole = True  # e.g. a mode change or binary file: be conservative
    return changed, unresolved


def _seed(db: Session, changed: dict[str, _Changed]) -> tuple[list[ChangedFile], dict[str, str]]:
    """Changed symbols and files as graph nodes. Returns {node id: changed path}."""
    seeds: dict[str, str] = {}
    out: list[ChangedFile] = []
    by_file: dict[uuid.UUID, list[Symbol]] = defaultdict(list)
    if changed:
        for symbol in db.scalars(
            select(Symbol)
            .where(Symbol.file_id.in_([c.file_id for c in changed.values()]))
            .order_by(Symbol.start_line, Symbol.id)
        ):
            by_file[symbol.file_id].append(symbol)
    for path in sorted(changed):
        c = changed[path]
        symbols = by_file[c.file_id]
        if c.whole:
            hit = list(symbols)
            module_level = True
        else:
            hit_ids = set(c.explicit_symbols)
            module_level = False
            for line in sorted(c.lines):
                inside = [s for s in symbols if s.start_line <= line <= s.end_line]
                if inside:  # the innermost symbol holding this line
                    hit_ids.add(min(inside, key=lambda s: (s.end_line - s.start_line, s.id)).id)
                elif not c.explicit_symbols:
                    module_level = True
            hit = [s for s in symbols if s.id in hit_ids]
        for symbol in hit:
            seeds[graph.symbol_node_id(symbol.id)] = path
        if module_level:
            seeds[graph.file_node_id(c.file_id)] = path
        out.append(
            ChangedFile(
                path=path,
                status=c.status,  # type: ignore[arg-type]
                lines=[] if c.whole else _ranges(c.lines),
                symbols=[graph.symbol_out(s, path) for s in hit],
                module_level=module_level,
            )
        )
    return out, seeds


def _ranges(lines: set[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for line in sorted(lines):
        if out and line == out[-1][1] + 1:
            out[-1][1] = line
        else:
            out.append([line, line])
    return out


# --- 2. who depends on it --------------------------------------------------------------------


def _walk(
    db: Session, snapshot_id: uuid.UUID, seeds: dict[str, str], request: ImpactRequest
) -> tuple[dict[str, _Reach], bool, bool]:
    """Breadth-first over incoming edges. Returns reach, truncated, and whether depth cut it off."""
    reach = {node: _Reach(0, 1.0, None, None, node) for node in seeds}
    frontier = sorted(seeds)
    truncated = False
    for level in range(1, request.depth + 1):
        if not frontier:
            return reach, truncated, False
        symbol_ids = [uuid.UUID(n[7:]) for n in frontier if n.startswith("symbol:")]
        file_ids = [uuid.UUID(n[5:]) for n in frontier if n.startswith("file:")]
        found: list[GraphEdge] = []
        if symbol_ids:
            found += graph.symbol_edges(
                db,
                snapshot_id,
                symbol_ids,
                direction="in",
                relations=graph.SYMBOL_RELATIONS,
                min_confidence=request.min_confidence,
                limit=MAX_VISITS,
            )
        if file_ids:
            found += graph.file_edges(
                db,
                snapshot_id,
                file_ids,
                direction="in",
                min_confidence=request.min_confidence,
                limit=MAX_VISITS,
            )
        truncated |= len(found) >= MAX_VISITS
        discovered: dict[str, _Reach] = {}
        for edge in found:
            source, target = graph._source_node(edge), graph._target_node(edge)
            if source in reach or target not in reach:
                continue
            parent = reach[target]
            confidence = parent.confidence * edge.confidence
            best = discovered.get(source)
            if best is None or confidence > best.confidence:
                discovered[source] = _Reach(level, confidence, edge, target, parent.changed)
        if len(reach) + len(discovered) > MAX_VISITS:
            keep = sorted(discovered, key=lambda n: (-discovered[n].confidence, n))
            discovered = {n: discovered[n] for n in keep[: max(MAX_VISITS - len(reach), 0)]}
            truncated = True
        reach.update(discovered)
        frontier = sorted(discovered)
    return reach, truncated, bool(frontier)


def _path(reach: dict[str, _Reach], node: str) -> list[str]:
    out = [node]
    current = reach[node].towards
    while current is not None:
        out.append(current)
        current = reach[current].towards
    return out


# --- 3. related tests ------------------------------------------------------------------------


def _heuristic_tests(
    db: Session,
    changed: dict[str, _Changed],
    changed_out: list[ChangedFile],
    files: dict[str, tuple[uuid.UUID, str | None]],
    already: set[str],
) -> list[RelatedTest]:
    test_files = {p: fid for p, (fid, _) in files.items() if is_test_path(p) and p not in already}
    out: list[RelatedTest] = []
    stems = {
        _subject_stem(p): p for p in sorted(changed, reverse=True) if not is_test_path(p)
    }  # sorted so the first path wins on a shared stem
    for path in sorted(test_files):
        subject = stems.get(_subject_stem(path))
        if subject is not None and subject != path:
            out.append(
                RelatedTest(
                    path=path,
                    node=None,
                    reason="name_match",
                    score=NAME_MATCH_SCORE,
                    distance=None,
                    detail=f"named after {subject}",
                )
            )
    named = {t.path for t in out}
    names = sorted(
        {
            s.name
            for c in changed_out
            if not is_test_path(c.path)
            for s in c.symbols
            if len(s.name) >= MIN_MENTION_NAME
        }
    )
    candidates = sorted(p for p in test_files if p not in named and p not in changed)
    if names and candidates:
        pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
        ids = {test_files[p]: p for p in candidates[:MAX_TEST_FILES_SCANNED]}
        texts = db.execute(
            select(FileContent.file_id, FileContent.text).where(FileContent.file_id.in_(ids))
        ).all()
        for file_id, text in sorted(texts, key=lambda row: ids[row[0]]):
            match = pattern.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                out.append(
                    RelatedTest(
                        path=ids[file_id],
                        node=None,
                        reason="mentions_symbol",
                        score=MENTION_SCORE,
                        distance=None,
                        detail=f"mentions {match.group(1)} at line {line}",
                    )
                )
    return out


# --- 4. what it could not see ----------------------------------------------------------------


def _limitations(
    changed: dict[str, _Changed],
    items: list[ImpactItem],
    tests: list[RelatedTest],
    graph_warnings: list[dict[str, object]],
    truncated: bool,
    depth_reached: bool,
    depth: int,
) -> list[str]:
    out: list[str] = []
    unsupported = sorted(p for p, c in changed.items() if c.language not in SUPPORTED_LANGUAGES)
    if unsupported:
        out.append(
            "These changed files are in languages the graph does not cover, so nothing that "
            f"depends on them was found: {', '.join(unsupported[:10])}"
            + (" and more." if len(unsupported) > 10 else ".")
        )
    if changed and not items:
        out.append(
            "Nothing in the graph depends on the changed code. It may be unused, an entry point, "
            "or reached only dynamically (routes, callbacks, configuration)."
        )
    if any(i.via.resolution == "inferred" for i in items):
        out.append("Some links are inferred from names, not proven; check their lines.")
    if depth_reached:
        out.append(f"Dependencies more than {depth} hops away were not followed.")
    if truncated:
        out.append("The result was cut short; raise min_confidence or narrow the change.")
    if graph_warnings:
        out.append(
            "The snapshot has imports the graph could not follow (see graph.warnings); code "
            "loaded that way is missing here."
        )
    if changed and not tests:
        out.append("No related tests were found by graph links, file names or symbol names.")
    out.append(ADVISORY)
    return out
