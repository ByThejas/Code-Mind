"""Retrieval evaluation: dataset loading, metrics, and a baseline run on the shop fixture."""

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from codemind.db.models import RepositorySnapshot
from codemind.evaluation.retrieval import (
    Case,
    evaluate,
    load_dataset,
    report_json,
    report_table,
    score_case,
)
from codemind.indexing.pipeline import IndexRun
from codemind.repositories.github import GitHubClient
from tests.conftest import TEST_SETTINGS
from tests.graph_fixtures import GRAPH_FILES
from tests.test_retrieval import HashEmbeddings, _ev

DATASETS = Path(__file__).resolve().parents[1] / "evaluation" / "datasets"
K = 6


@pytest.fixture
def repo_files() -> dict[str, bytes]:
    return GRAPH_FILES


def test_datasets_load_and_expect_real_fixture_files() -> None:
    for path in sorted(DATASETS.glob("*.json")):
        dataset = load_dataset(path)
        assert dataset.cases, path
    shop = load_dataset(DATASETS / "shop-fixture.v1.json")
    for case in shop.cases:
        assert set(case.expected_files) <= set(GRAPH_FILES), case.id


def test_dataset_validation(tmp_path: Path) -> None:
    case = {"id": "a", "category": "flow", "question": "q?", "expected_files": ["x.py"]}
    bad = {"name": "n", "version": 1, "repository": "o/r", "cases": [case, case]}
    path = tmp_path / "d.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="unique"):
        load_dataset(path)
    bad["cases"] = [case | {"category": "vibes"}]
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="category"):
        load_dataset(path)


def test_metrics() -> None:
    case = Case("c", "flow", "q", ["a.py", "b.py"], ["f", "g"])
    evidence = [_ev("z.py", 1, 5), _ev("a.py", 1, 5), _ev("a.py", 9, 12)]
    evidence[1].symbol_name = "Thing.f"
    result = score_case(case, evidence)
    assert result.recall == 0.5
    assert result.precision == pytest.approx(2 / 3)
    assert result.reciprocal_rank == 0.5
    assert result.symbol_recall == 0.5  # "Thing.f" counts for "f"
    assert (result.found, result.missed) == (["a.py"], ["b.py"])
    empty = score_case(case, [])
    assert (empty.recall, empty.precision, empty.reciprocal_rank) == (0, 0, 0)


def _indexed_snapshot(client, session_factory, enqueued, db: Session, embedder) -> uuid.UUID:
    client.post("/api/v1/repositories", json={"url": "owner/repo"})
    while enqueued:
        IndexRun(
            uuid.UUID(enqueued.pop(0)),
            session_factory=session_factory,
            github_factory=lambda: GitHubClient(TEST_SETTINGS),
            settings=TEST_SETTINGS,
            embedder_factory=lambda: embedder,
        ).run()
    snapshot = db.scalar(select(RepositorySnapshot))
    assert snapshot is not None
    return snapshot.id


def test_baseline_on_the_shop_fixture(client, session_factory, enqueued, db: Session) -> None:
    """The recorded baseline (docs/evaluation.md). Update both when retrieval changes."""
    embedder = HashEmbeddings()
    snapshot_id = _indexed_snapshot(client, session_factory, enqueued, db, embedder)
    dataset = load_dataset(DATASETS / "shop-fixture.v1.json")
    results = evaluate(
        db,
        snapshot_id,
        dataset,
        embedder=embedder,
        k=K,
        token_budget=TEST_SETTINGS.answer_context_tokens,
    )
    print("\n" + report_table(results))
    by_name = {r.config: r for r in results}
    assert [r.config for r in results] == ["keyword", "keyword_graph", "hybrid", "hybrid_graph"]
    assert all(r.skipped is None for r in results)
    assert by_name["hybrid"].vector_search == "used"

    # Lookups by name are what keyword and symbol search are for.
    assert by_name["keyword"].by_category["symbol_lookup"]["recall"] == 1.0
    assert by_name["keyword"].by_category["symbol_lookup"]["mrr"] == 1.0
    # Graph expansion must help flow questions and not hurt overall.
    flow = {name: r.by_category["flow"]["recall"] for name, r in by_name.items()}
    assert flow["hybrid_graph"] > flow["hybrid"], flow
    assert flow["keyword_graph"] > flow["keyword"], flow
    assert by_name["hybrid_graph"].summary["recall"] >= by_name["hybrid"].summary["recall"]
    assert by_name["hybrid_graph"].summary["recall"] >= 0.8

    report = report_json(dataset, results, k=K)
    assert report["dataset"]["cases"] == len(dataset.cases)
    json.dumps(report)  # serialisable for --json


def test_vector_configs_are_skipped_without_embeddings(
    client, session_factory, enqueued, db: Session
) -> None:
    snapshot_id = _indexed_snapshot(client, session_factory, enqueued, db, None)
    dataset = load_dataset(DATASETS / "shop-fixture.v1.json")
    no_provider = evaluate(db, snapshot_id, dataset, embedder=None, k=K, token_budget=8000)
    assert [r.skipped for r in no_provider] == [None, None] + ["no_embedding_provider"] * 2
    not_embedded = evaluate(
        db, snapshot_id, dataset, embedder=HashEmbeddings(), k=K, token_budget=8000
    )
    assert [r.skipped for r in not_embedded][2:] == ["vector_search_no_embeddings"] * 2
