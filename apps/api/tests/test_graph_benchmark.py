"""Flow benchmark (blueprint Phase 5 exit gate): graph expansion vs the Phase 4 retrieval baseline.

Each case is a question whose answer needs code from several files that call each other. The score
is recall: the share of the needed files that appear in the evidence sent to the answer model.
Embeddings are the deterministic bag-of-words fake from test_retrieval, so this runs in CI.
Results are recorded in docs/graph-design.md; update them there when retrieval changes.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from codemind.db.models import RepositorySnapshot
from codemind.indexing.pipeline import IndexRun
from codemind.repositories.github import GitHubClient
from codemind.retrieval.answer import build_user_prompt
from codemind.retrieval.search import retrieve
from tests.conftest import TEST_SETTINGS
from tests.graph_fixtures import FLOW_CASES, GRAPH_FILES
from tests.test_retrieval import HashEmbeddings

MAX_ITEMS = 6


@pytest.fixture
def repo_files() -> dict[str, bytes]:
    return GRAPH_FILES


def test_graph_expansion_beats_baseline_on_flow_questions(
    client, session_factory, enqueued, db: Session
) -> None:
    embedder = HashEmbeddings()
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

    def recall(question: str, expected: set[str], use_graph: bool) -> float:
        result = retrieve(
            db,
            snapshot.id,
            question,
            embedder=embedder,
            token_budget=TEST_SETTINGS.answer_context_tokens,
            max_items=MAX_ITEMS,
            use_graph=use_graph,
        )
        return len({e.path for e in result.evidence} & expected) / len(expected)

    rows = [
        (question, recall(question, expected, False), recall(question, expected, True))
        for question, expected in FLOW_CASES
    ]
    report = "\n".join(f"{base:.2f} -> {graph:.2f}  {q}" for q, base, graph in rows)
    print(f"\nflow recall@{MAX_ITEMS} (baseline -> graph)\n{report}")
    baseline = sum(r[1] for r in rows) / len(rows)
    with_graph = sum(r[2] for r in rows) / len(rows)
    assert all(graph >= base for _, base, graph in rows), report
    assert with_graph > baseline, report


def test_prompt_lists_relationships_between_evidence(client, session_factory, enqueued, db) -> None:
    client.post("/api/v1/repositories", json={"url": "owner/repo"})
    while enqueued:
        IndexRun(
            uuid.UUID(enqueued.pop(0)),
            session_factory=session_factory,
            github_factory=lambda: GitHubClient(TEST_SETTINGS),
            settings=TEST_SETTINGS,
        ).run()
    snapshot = db.scalar(select(RepositorySnapshot))
    result = retrieve(
        db,
        snapshot.id,
        "What happens when create_order_endpoint is called?",
        embedder=None,
        token_budget=TEST_SETTINGS.answer_context_tokens,
        max_items=MAX_ITEMS,
    )
    assert "graph" in {s for e in result.evidence for s in e.retrieved_by}
    described = [r.describe() for r in result.relations]
    assert any("create_order_endpoint calls" in d and "place_order" in d for d in described)
    prompt = build_user_prompt("q", result.evidence, result.relations)
    assert "Relationships between the evidence (static analysis):" in prompt
