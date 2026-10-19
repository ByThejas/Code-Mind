"""Imports, calls and inheritance: parsing, resolution into graph edges, and the graph endpoints."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from codemind.db.models import GraphEdge, RepositorySnapshot
from codemind.graph.build import build_graph
from codemind.indexing.pipeline import IndexRun, _graph_input
from codemind.parsing.treesitter import parse_symbols
from codemind.repositories.github import GitHubClient
from tests.conftest import TEST_SETTINGS
from tests.graph_fixtures import GRAPH_FILES

LANGUAGES = {".py": "python", ".ts": "typescript", ".tsx": "tsx"}


@pytest.fixture
def repo_files() -> dict[str, bytes]:
    return GRAPH_FILES


def _index(client: TestClient, session_factory: sessionmaker[Session], enqueued: list[str]) -> str:
    repo_id = client.post("/api/v1/repositories", json={"url": "owner/repo"}).json()["repository"][
        "id"
    ]
    while enqueued:
        IndexRun(
            uuid.UUID(enqueued.pop(0)),
            session_factory=session_factory,
            github_factory=lambda: GitHubClient(TEST_SETTINGS),
            settings=TEST_SETTINGS,
        ).run()
    return str(repo_id)


def _symbol_id(client: TestClient, repo_id: str, name: str) -> str:
    items = client.get(f"/api/v1/repositories/{repo_id}/symbols", params={"q": name}).json()[
        "items"
    ]
    return next(s["id"] for s in items if s["qualified_name"] == name)


# Parsing


def test_parses_python_imports_and_references() -> None:
    result = parse_symbols("python", GRAPH_FILES["shop/api/routes.py"].decode())
    imports = [(i.module, i.level, i.bindings, i.module_alias) for i in result.imports]
    assert imports == [
        ("shop.services", 0, [("orders", "orders")], None),
        ("billing", 2, [("refund", "send_refund")], None),
        ("importlib", 0, [], "importlib"),
    ]
    calls = [
        (r.name, r.receiver, r.source.qualified_name if r.source else None)
        for r in result.references
    ]
    assert ("place_order", "orders", "create_order_endpoint") in calls
    assert ("send_refund", None, "cancel_order_endpoint") in calls
    assert result.dynamic_imports == 1  # importlib.import_module(name)

    store = parse_symbols("python", GRAPH_FILES["shop/db/store.py"].decode())
    extends = [
        (r.name, r.source.qualified_name) for r in store.references if r.relation == "extends"
    ]
    assert extends == [("Repository", "OrderRepository")]


def test_parses_typescript_imports_and_references() -> None:
    result = parse_symbols("tsx", GRAPH_FILES["web/src/Checkout.tsx"].decode())
    assert [(i.module, i.bindings) for i in result.imports] == [
        ("./api", [("submitOrder", "submitOrder")]),
        ("./checkout.css", []),
        ("@/lib/analytics", [("track", "track")]),
    ]
    api = parse_symbols("typescript", GRAPH_FILES["web/src/api.ts"].decode())
    assert api.imports[1].module_alias == "http"
    calls = {(r.name, r.receiver) for r in api.references}
    assert {("post", "http"), ("formatPrice", None)} <= calls
    assert parse_symbols("typescript", GRAPH_FILES["web/src/lazy.ts"].decode()).dynamic_imports == 1


def test_one_line_class_attributes_bases_to_the_class() -> None:
    result = parse_symbols("typescript", "class C extends D implements E { m() { this.n() } }")
    by_relation = {r.relation: r.source.qualified_name for r in result.references}
    assert by_relation == {"extends": "C", "implements": "C", "calls": "C.m"}


# Resolution


def _build() -> tuple[list[dict], dict[uuid.UUID, str], dict]:
    inputs, names = [], {}
    for path, data in GRAPH_FILES.items():
        language = next((lang for ext, lang in LANGUAGES.items() if path.endswith(ext)), None)
        if language is None:
            continue
        result = parse_symbols(language, data.decode())
        ids = {id(s): uuid.uuid4() for s in result.symbols}
        file_id = uuid.uuid4()
        names[file_id] = path
        names.update({ids[id(s)]: s.qualified_name for s in result.symbols})
        inputs.append(_graph_input(file_id, path, language, result, ids))
    built = build_graph(uuid.uuid4(), inputs)
    return built.edges, names, {"stats": built.stats, "warnings": built.warnings}


def test_resolves_edges_with_provenance() -> None:
    edges, names, summary = _build()

    def label(e: dict) -> tuple[str, str, str, str]:
        src = names[e["source_symbol_id"] or e["source_file_id"]]
        tgt = names[e["target_symbol_id"] or e["target_file_id"]]
        return (e["relation"], src, tgt, e["provenance"].rsplit(":", 1)[-1])

    found = {label(e) for e in edges}
    expected = {
        ("imports", "shop/api/routes.py", "shop/services/orders.py", "import-module"),
        ("imports", "shop/api/routes.py", "shop/billing.py", "import"),  # from ..billing
        ("imports", "web/src/Checkout.tsx", "web/src/api.ts", "import"),
        ("calls", "create_order_endpoint", "place_order", "module-attribute"),
        ("calls", "cancel_order_endpoint", "refund", "imported-name"),  # through an alias
        ("calls", "place_order", "charge", "imported-name"),
        ("calls", "place_order", "OrderRepository", "imported-name"),
        ("calls", "place_order", "OrderRepository.store_order", "method-name-match"),
        ("calls", "charge", "gateway_request", "local"),
        ("calls", "OrderService.run", "place_order", "local"),
        ("calls", "OrderService.run", "BaseService.log", "inherited-method"),
        ("calls", "OrderRepository.store_order", "Repository.persist", "inherited-method"),
        ("extends", "OrderService", "BaseService", "imported-name"),
        ("extends", "OrderRepository", "Repository", "local"),
        ("calls", "submitOrder", "post", "module-attribute"),
        ("calls", "submitOrder", "formatPrice", "imported-name"),
        ("calls", "Checkout.onPay", "submitOrder", "imported-name"),
    }
    assert expected <= found
    by_label = {label(e): e for e in edges}
    guess = by_label[("calls", "place_order", "OrderRepository.store_order", "method-name-match")]
    assert (guess["resolution"], guess["confidence"]) == ("inferred", 0.3)
    exact = by_label[("calls", "charge", "gateway_request", "local")]
    assert (exact["resolution"], exact["line"]) == ("static", 3)

    # Library calls, builtins and stylesheet imports are not edges.
    assert not any(names.get(e["target_symbol_id"]) in ("print", "sum") for e in edges)
    stats = summary["stats"]
    assert stats["dynamic_imports"] == 2
    assert stats["imports_unresolved"] == 1  # @/lib/analytics
    codes = {w["code"]: w for w in summary["warnings"]}
    assert set(codes) == {"graph_dynamic_imports", "graph_unresolved_imports"}
    assert codes["graph_unresolved_imports"]["imports"] == [
        "web/src/Checkout.tsx:3 @/lib/analytics"
    ]


def test_common_method_names_are_not_guessed() -> None:
    py = "def f(items):\n    items.append(1)\n\n\nclass Box:\n    def append(self, x):\n        pass\n"
    result = parse_symbols("python", py)
    ids = {id(s): uuid.uuid4() for s in result.symbols}
    built = build_graph(uuid.uuid4(), [_graph_input(uuid.uuid4(), "a.py", "python", result, ids)])
    assert built.edges == []


# Indexing and API


def test_index_stores_graph_and_reindex_rewrites_it(
    client: TestClient, session_factory, enqueued, db: Session
) -> None:
    repo_id = _index(client, session_factory, enqueued)
    snapshot = db.scalar(select(RepositorySnapshot))
    assert snapshot.status == "ready"  # graph warnings do not make a snapshot partial
    graph = snapshot.stats["graph"]
    assert graph["edges"] == db.scalar(select(func.count()).select_from(GraphEdge)) > 20
    assert {w["code"] for w in snapshot.warnings} == {
        "graph_dynamic_imports",
        "graph_unresolved_imports",
    }

    client.post(f"/api/v1/repositories/{repo_id}/index", json={"force": True})
    _index_pending(session_factory, enqueued)
    assert db.scalar(select(func.count()).select_from(GraphEdge)) == graph["edges"]


def _index_pending(session_factory, enqueued: list[str]) -> None:
    while enqueued:
        IndexRun(
            uuid.UUID(enqueued.pop(0)),
            session_factory=session_factory,
            github_factory=lambda: GitHubClient(TEST_SETTINGS),
            settings=TEST_SETTINGS,
        ).run()


def test_symbol_search_and_detail(client: TestClient, session_factory, enqueued) -> None:
    repo_id = _index(client, session_factory, enqueued)
    base = f"/api/v1/repositories/{repo_id}"
    items = client.get(f"{base}/symbols", params={"q": "place"}).json()["items"]
    assert items[0]["qualified_name"] == "place_order"
    classes = client.get(f"{base}/symbols", params={"kind": "class", "path_prefix": "shop/db"})
    assert [s["name"] for s in classes.json()["items"]] == ["Repository", "OrderRepository"]

    detail = client.get(f"{base}/symbols/{items[0]['id']}").json()
    nodes = {n["id"]: n for n in detail["nodes"]}
    callers = {nodes[e["source"]]["qualified_name"] for e in detail["incoming"]}
    callees = {nodes[e["target"]]["qualified_name"] for e in detail["outgoing"]}
    assert callers == {"create_order_endpoint", "OrderService.run"}
    assert callees == {"charge", "OrderRepository", "OrderRepository.store_order"}
    assert detail["graph"]["available"] is True
    call = next(e for e in detail["incoming"] if e["path"] == "shop/api/routes.py")
    assert (call["line"], call["resolution"]) == (8, "static")

    confident = client.get(f"{base}/symbols/{items[0]['id']}", params={"min_confidence": 0.5})
    assert len(confident.json()["outgoing"]) == 2

    missing = client.get(f"{base}/symbols/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "symbol_not_found"


def test_neighborhood_by_symbol_and_file(client: TestClient, session_factory, enqueued) -> None:
    repo_id = _index(client, session_factory, enqueued)
    base = f"/api/v1/repositories/{repo_id}"
    start = _symbol_id(client, repo_id, "create_order_endpoint")
    out = client.get(
        f"{base}/graph/neighborhood",
        params={"symbol_id": start, "direction": "out", "depth": 3, "min_confidence": 0.5},
    ).json()
    names = [n["qualified_name"] for n in out["nodes"]]
    assert names[0] == "create_order_endpoint"
    assert {"place_order", "charge", "gateway_request", "OrderRepository"} <= set(names)
    assert "OrderRepository.store_order" not in names  # below min_confidence

    files = client.get(
        f"{base}/graph/neighborhood",
        params={"path": "shop/services/orders.py", "direction": "both"},
    ).json()
    paths = {n["path"] for n in files["nodes"]}
    assert paths == {
        "shop/services/orders.py",
        "shop/billing.py",
        "shop/db/store.py",
        "shop/services/base.py",
        "shop/api/routes.py",
    }
    assert {e["relation"] for e in files["edges"]} == {"imports"}

    capped = client.get(
        f"{base}/graph/neighborhood", params={"symbol_id": start, "depth": 3, "max_nodes": 2}
    ).json()
    assert len(capped["nodes"]) == 2 and capped["truncated"] is True

    both = client.get(f"{base}/graph/neighborhood", params={"symbol_id": start, "path": "x"})
    assert both.status_code == 422


def test_trace_follows_calls_across_files(client: TestClient, session_factory, enqueued) -> None:
    repo_id = _index(client, session_factory, enqueued)
    base = f"/api/v1/repositories/{repo_id}"
    start = _symbol_id(client, repo_id, "create_order_endpoint")
    end = _symbol_id(client, repo_id, "gateway_request")
    result = client.get(
        f"{base}/graph/trace", params={"from_symbol_id": start, "to_symbol_id": end}
    ).json()
    assert result["found"] is True
    steps = result["steps"]
    assert [s["node"]["qualified_name"] for s in steps] == [
        "create_order_endpoint",
        "place_order",
        "charge",
        "gateway_request",
    ]
    assert steps[0]["via"] is None
    assert steps[2]["source_line"] == "charge(user, total)"
    assert steps[2]["via"]["path"] == "shop/services/orders.py"

    back = client.get(
        f"{base}/graph/trace", params={"from_symbol_id": end, "to_symbol_id": start}
    ).json()
    assert back["found"] is False and back["steps"] == []
    assert "No chain" in back["limitations"][0]


def test_module_graph(client: TestClient, session_factory, enqueued) -> None:
    repo_id = _index(client, session_factory, enqueued)
    result = client.get(f"/api/v1/repositories/{repo_id}/graph/modules", params={"depth": 2})
    body = result.json()
    nodes = {n["path"]: n["file_count"] for n in body["nodes"]}
    assert nodes["shop/services"] == 3 and nodes["web/src"] == 5
    edges = {(e["source"], e["target"]): e["count"] for e in body["edges"]}
    # routes.py imports shop/services/__init__.py and shop/services/orders.py
    assert edges[("module:shop/api", "module:shop/services")] == 2
    assert edges[("module:shop/services", "module:shop/db")] == 1
    assert ("module:web/src", "module:web/src") not in edges


def test_snapshot_without_graph_says_so(
    client: TestClient, session_factory, enqueued, db: Session
) -> None:
    repo_id = _index(client, session_factory, enqueued)
    snapshot = db.scalar(select(RepositorySnapshot))
    snapshot.stats = {k: v for k, v in snapshot.stats.items() if k != "graph"}
    db.commit()
    symbol = _symbol_id(client, repo_id, "charge")

    detail = client.get(f"/api/v1/repositories/{repo_id}/symbols/{symbol}").json()
    assert detail["graph"]["available"] is False
    assert detail["graph"]["warnings"][0]["code"] == "graph_not_built"
    trace = client.get(
        f"/api/v1/repositories/{repo_id}/graph/trace",
        params={"from_symbol_id": symbol, "to_symbol_id": symbol},
    )
    assert trace.status_code == 409
    assert trace.json()["error"]["code"] == "graph_not_built"
