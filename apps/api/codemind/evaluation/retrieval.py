"""Retrieval evaluation (blueprint §17): run a fixed question set and measure what search finds.

A dataset is a versioned JSON file of questions, each with the files (and optionally symbols) a good
answer needs. Every case runs under several retrieval configurations so each technique is compared
with the simpler one before it, on the same questions:

    keyword        keyword, symbol-name and path search only
    keyword_graph  + graph expansion (what investigations do without Ollama)
    hybrid         keyword + vector search (needs an embedding provider and embeddings)
    hybrid_graph   + graph expansion (what investigations do with Ollama)

Metrics are deterministic and need no answer model. See docs/evaluation.md.
"""

import json
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from codemind.providers import EmbeddingProvider
from codemind.retrieval.search import Evidence, retrieve

CATEGORIES = ("symbol_lookup", "explanation", "flow", "config")


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    question: str
    expected_files: list[str]
    expected_symbols: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Dataset:
    name: str
    version: int
    repository: str  # owner/name, or "fixture:<name>" for test fixtures
    commit_sha: str | None  # the commit the expectations were written against
    cases: list[Case]


@dataclass(frozen=True)
class RunConfig:
    name: str
    use_vector: bool
    use_graph: bool


CONFIGS = (
    RunConfig("keyword", use_vector=False, use_graph=False),
    RunConfig("keyword_graph", use_vector=False, use_graph=True),
    RunConfig("hybrid", use_vector=True, use_graph=False),
    RunConfig("hybrid_graph", use_vector=True, use_graph=True),
)


@dataclass
class CaseResult:
    case_id: str
    category: str
    recall: float  # share of expected files in the evidence
    precision: float  # share of evidence items from an expected file
    reciprocal_rank: float  # 1 / rank of the first evidence item from an expected file, else 0
    symbol_recall: float | None  # share of expected symbols named by an evidence chunk
    found: list[str]
    missed: list[str]
    latency_ms: float


@dataclass
class ConfigResult:
    config: str
    skipped: str | None  # why the config did not run, e.g. no embedding provider
    vector_search: str | None
    summary: dict[str, float]
    by_category: dict[str, dict[str, float]]
    cases: list[CaseResult]


def load_dataset(path: Path) -> Dataset:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = [Case(**c) for c in raw["cases"]]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: case ids must be unique")
    for case in cases:
        if case.category not in CATEGORIES:
            raise ValueError(f"{path}: {case.id} has unknown category {case.category!r}")
        if not case.expected_files:
            raise ValueError(f"{path}: {case.id} lists no expected files")
    return Dataset(
        name=raw["name"],
        version=int(raw["version"]),
        repository=raw["repository"],
        commit_sha=raw.get("commit_sha"),
        cases=cases,
    )


def score_case(case: Case, evidence: list[Evidence], latency_ms: float = 0.0) -> CaseResult:
    expected = set(case.expected_files)
    paths = [e.path for e in evidence]
    found = sorted(expected & set(paths))
    rank = next((i + 1 for i, p in enumerate(paths) if p in expected), None)
    symbol_recall = None
    if case.expected_symbols:
        named = {e.symbol_name for e in evidence if e.symbol_name}
        short = {n.rsplit(".", 1)[-1] for n in named}
        hits = [s for s in case.expected_symbols if s in named or s in short]
        symbol_recall = len(hits) / len(case.expected_symbols)
    return CaseResult(
        case_id=case.id,
        category=case.category,
        recall=len(found) / len(expected),
        precision=sum(p in expected for p in paths) / len(paths) if paths else 0.0,
        reciprocal_rank=1 / rank if rank else 0.0,
        symbol_recall=symbol_recall,
        found=found,
        missed=sorted(expected - set(paths)),
        latency_ms=round(latency_ms, 1),
    )


def summarize(results: list[CaseResult]) -> dict[str, float]:
    if not results:
        return {}
    n = len(results)
    symbol = [r.symbol_recall for r in results if r.symbol_recall is not None]
    latencies = sorted(r.latency_ms for r in results)
    out = {
        "cases": float(n),
        "recall": sum(r.recall for r in results) / n,
        "precision": sum(r.precision for r in results) / n,
        "mrr": sum(r.reciprocal_rank for r in results) / n,
        "hit_rate": sum(r.recall > 0 for r in results) / n,
        "full_recall_rate": sum(r.recall == 1 for r in results) / n,
        "latency_ms_p50": latencies[n // 2],
    }
    if symbol:
        out["symbol_recall"] = sum(symbol) / len(symbol)
    return {k: round(v, 4) for k, v in out.items()}


def evaluate(
    db: Session,
    snapshot_id: uuid.UUID,
    dataset: Dataset,
    *,
    embedder: EmbeddingProvider | None,
    k: int,
    token_budget: int,
    configs: tuple[RunConfig, ...] = CONFIGS,
) -> list[ConfigResult]:
    out: list[ConfigResult] = []
    for config in configs:
        if config.use_vector and embedder is None:
            out.append(ConfigResult(config.name, "no_embedding_provider", None, {}, {}, []))
            continue
        results: list[CaseResult] = []
        vector_search: str | None = None
        for case in dataset.cases:
            started = time.perf_counter()
            retrieval = retrieve(
                db,
                snapshot_id,
                case.question,
                embedder=embedder if config.use_vector else None,
                token_budget=token_budget,
                max_items=k,
                use_graph=config.use_graph,
            )
            elapsed = (time.perf_counter() - started) * 1000
            if config.use_vector:
                vector_search = retrieval.vector_search
            results.append(score_case(case, retrieval.evidence, elapsed))
        if config.use_vector and vector_search != "used":
            # Without embeddings this run equals "keyword"; reporting it would mislead.
            out.append(
                ConfigResult(config.name, f"vector_search_{vector_search}", None, {}, {}, [])
            )
            continue
        groups: dict[str, list[CaseResult]] = defaultdict(list)
        for result in results:
            groups[result.category].append(result)
        out.append(
            ConfigResult(
                config=config.name,
                skipped=None,
                vector_search=vector_search,
                summary=summarize(results),
                by_category={c: summarize(g) for c, g in sorted(groups.items())},
                cases=results,
            )
        )
    return out


def report_json(dataset: Dataset, results: list[ConfigResult], **meta: Any) -> dict[str, Any]:
    return {
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "repository": dataset.repository,
            "commit_sha": dataset.commit_sha,
            "cases": len(dataset.cases),
        },
        **meta,
        "results": [asdict(r) for r in results],
    }


def report_table(results: list[ConfigResult]) -> str:
    """A plain-text table of the summary metrics, one row per configuration."""
    columns = ("recall", "precision", "mrr", "hit_rate", "full_recall_rate", "latency_ms_p50")
    lines = [f"{'config':<14}" + "".join(f"{c:>18}" for c in columns)]
    for r in results:
        if r.skipped:
            lines.append(f"{r.config:<14}  skipped: {r.skipped}")
            continue
        lines.append(f"{r.config:<14}" + "".join(f"{r.summary.get(c, 0):>18.3f}" for c in columns))
    return "\n".join(lines)
