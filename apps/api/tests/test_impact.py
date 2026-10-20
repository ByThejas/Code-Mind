"""Impact analysis: reading diffs, walking dependants, related tests and stated limitations."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from codemind.db.models import RepositorySnapshot
from codemind.impact.analysis import is_test_path
from codemind.impact.diff import parse_unified_diff
from tests.graph_fixtures import IMPACT_FILES
from tests.test_graph import _index, _symbol_id


@pytest.fixture
def repo_files() -> dict[str, bytes]:
    return IMPACT_FILES


def _impact(client: TestClient, repo_id: str, **body) -> dict:
    response = client.post(f"/api/v1/repositories/{repo_id}/impact-analysis", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _names(result: dict) -> dict[str, int]:
    return {
        i["node"]["qualified_name"] or i["node"]["path"]: i["distance"] for i in result["impacted"]
    }


# Diffs

GIT_DIFF = """diff --git a/shop/billing.py b/shop/billing.py
index 1111111..2222222 100644
--- a/shop/billing.py
+++ b/shop/billing.py
@@ -1,4 +1,5 @@
 def charge(user, amount):
     \"\"\"Take payment through the card gateway.\"\"\"
-    return gateway_request(user.card, amount)
+    assert amount > 0
+    return gateway_request(user.card, amount)
 
diff --git a/docs/old.md b/docs/new.md
similarity index 100%
rename from docs/old.md
rename to docs/new.md
diff --git a/shop/db/store.py b/shop/db/store.py
deleted file mode 100644
--- a/shop/db/store.py
+++ /dev/null
@@ -1,2 +0,0 @@
-class Repository:
-    pass
diff --git a/shop/new.py b/shop/new.py
new file mode 100644
--- /dev/null
+++ b/shop/new.py
@@ -0,0 +1 @@
+--- not a header
"""


def test_parses_git_diff_into_old_side_lines() -> None:
    changes = {c.path: c for c in parse_unified_diff(GIT_DIFF)}
    billing = changes["shop/billing.py"]
    assert (billing.status, billing.lines) == ("modified", {3})
    assert changes["docs/old.md"].status == "renamed"
    assert changes["docs/old.md"].new_path == "docs/new.md"
    assert (changes["shop/db/store.py"].status, changes["shop/db/store.py"].lines) == (
        "deleted",
        {1, 2},
    )
    assert changes["shop/new.py"].status == "added"
    assert len(changes) == 4  # the added "--- not a header" line stayed inside its hunk


def test_parses_plain_unified_diff() -> None:
    diff = "--- a.py\t2026-01-01\n+++ a.py\t2026-01-02\n@@ -10,2 +10,3 @@\n x\n+y\n z\n"
    [change] = parse_unified_diff(diff)
    assert (change.path, change.status, change.lines) == ("a.py", "modified", {10})


def test_recognises_test_files() -> None:
    for path in ("tests/x.py", "pkg/test_a.py", "a_test.py", "src/a.spec.tsx", "__tests__/b.js"):
        assert is_test_path(path), path
    for path in ("src/testing.py", "contest.py", "src/latest.ts"):
        assert not is_test_path(path), path


# Analysis


def test_changing_a_function_finds_callers_and_its_test(
    client: TestClient, session_factory, enqueued
) -> None:
    repo_id = _index(client, session_factory, enqueued)
    charge = _symbol_id(client, repo_id, "charge")
    result = _impact(client, repo_id, targets=[{"symbol_id": charge}])

    [changed] = result["changed"]
    assert changed["path"] == "shop/billing.py"
    assert [s["qualified_name"] for s in changed["symbols"]] == ["charge"]
    assert _names(result) == {"place_order": 1, "create_order_endpoint": 2, "OrderService.run": 2}
    first = result["impacted"][0]
    assert first["node"]["qualified_name"] == "place_order"
    assert first["source_line"] == "charge(user, total)"
    assert first["via"]["line"] == 8
    assert first["path_to_change"] == [first["node"]["id"], f"symbol:{charge}"]
    scores = [i["score"] for i in result["impacted"]]
    assert scores == sorted(scores, reverse=True) and scores[-1] < scores[0]
    assert result["files"][0]["path"] == "shop/services/orders.py"

    [test] = result["tests"]
    assert (test["path"], test["reason"]) == ("tests/test_billing.py", "calls_changed_code")
    assert test["node"]["label"] == "test_charge_keeps_the_last_four_digits"
    assert result["advisory"] is True
    assert "advisory" in result["limitations"][-1]


def test_a_diff_resolves_to_the_symbols_it_touches(
    client: TestClient, session_factory, enqueued
) -> None:
    repo_id = _index(client, session_factory, enqueued)
    result = _impact(client, repo_id, diff=GIT_DIFF)
    changed = {c["path"]: c for c in result["changed"]}
    assert changed["shop/billing.py"]["lines"] == [[3, 3]]
    assert [s["name"] for s in changed["shop/billing.py"]["symbols"]] == ["charge"]
    assert changed["shop/billing.py"]["module_level"] is False
    assert changed["shop/db/store.py"]["status"] == "deleted"
    assert changed["shop/db/store.py"]["module_level"] is True  # the whole file goes
    names = _names(result)
    assert names["place_order"] == 1  # calls charge and constructs OrderRepository
    assert names["shop/services/orders.py"] == 1  # imports the deleted file
    reasons = {(u["path"], u["reason"]) for u in result["unresolved"]}
    assert reasons == {("docs/old.md", "not_indexed"), ("shop/new.py", "new_file")}


def test_whole_file_change_follows_importers_and_finds_tests(
    client: TestClient, session_factory, enqueued
) -> None:
    repo_id = _index(client, session_factory, enqueued)
    result = _impact(client, repo_id, targets=[{"path": "web/src/api.ts"}])
    assert {(i["node"]["type"], i["node"]["path"]) for i in result["impacted"]} == {
        ("file", "web/src/Checkout.tsx"),
        ("symbol", "web/src/Checkout.tsx"),
    }
    assert [(t["path"], t["reason"]) for t in result["tests"]] == [
        ("web/src/api.test.ts", "imports_changed_code")
    ]

    orders = _impact(
        client, repo_id, targets=[{"path": "shop/services/orders.py", "start_line": 6}]
    )
    assert [s["name"] for s in orders["changed"][0]["symbols"]] == ["place_order"]
    tests = {t["path"]: t["reason"] for t in orders["tests"]}
    assert tests == {"tests/test_orders.py": "name_match", "tests/test_flows.py": "mentions_symbol"}


def test_depth_confidence_and_result_limits(client: TestClient, session_factory, enqueued) -> None:
    repo_id = _index(client, session_factory, enqueued)
    gateway = _symbol_id(client, repo_id, "gateway_request")
    shallow = _impact(client, repo_id, targets=[{"symbol_id": gateway}], depth=1)
    assert set(_names(shallow)) == {"charge", "refund"}
    assert any("hops away were not followed" in x for x in shallow["limitations"])

    deep = _impact(client, repo_id, targets=[{"symbol_id": gateway}], depth=6)
    assert {"place_order", "cancel_order_endpoint", "create_order_endpoint"} <= set(_names(deep))
    assert not any("hops away" in x for x in deep["limitations"])

    capped = _impact(client, repo_id, targets=[{"symbol_id": gateway}], depth=6, max_results=2)
    assert len(capped["impacted"]) == 2 and capped["truncated"] is True

    store = _symbol_id(client, repo_id, "OrderRepository.store_order")
    guessed = _impact(client, repo_id, targets=[{"symbol_id": store}])
    assert "place_order" in _names(guessed)  # a method-name guess, confidence 0.3
    assert any("inferred" in x for x in guessed["limitations"])
    strict = _impact(client, repo_id, targets=[{"symbol_id": store}], min_confidence=0.5)
    assert "place_order" not in _names(strict)


def test_result_is_reproducible(client: TestClient, session_factory, enqueued) -> None:
    repo_id = _index(client, session_factory, enqueued)
    body = {"diff": GIT_DIFF, "targets": [{"path": "web/src/format.ts"}]}
    first, second = _impact(client, repo_id, **body), _impact(client, repo_id, **body)
    assert first == second


def test_unsupported_and_unknown_targets_are_reported(
    client: TestClient, session_factory, enqueued
) -> None:
    repo_id = _index(client, session_factory, enqueued)
    result = _impact(
        client,
        repo_id,
        targets=[
            {"path": "README.md"},
            {"path": "nope.py"},
            {"symbol_id": "00000000-0000-0000-0000-000000000000"},
        ],
    )
    assert result["impacted"] == []
    assert {u["reason"] for u in result["unresolved"]} == {"not_indexed", "symbol_not_found"}
    text = " ".join(result["limitations"])
    assert "README.md" in text and "Nothing in the graph depends" in text


def test_rejects_bad_requests_and_snapshots_without_a_graph(
    client: TestClient, session_factory, enqueued, db: Session
) -> None:
    repo_id = _index(client, session_factory, enqueued)
    url = f"/api/v1/repositories/{repo_id}/impact-analysis"
    assert client.post(url, json={}).status_code == 422
    assert (
        client.post(
            url, json={"targets": [{"path": "a.py", "symbol_id": None}], "depth": 9}
        ).status_code
        == 422
    )
    both = {"targets": [{"path": "a.py", "symbol_id": "00000000-0000-0000-0000-000000000000"}]}
    assert client.post(url, json=both).status_code == 422

    snapshot = db.scalar(select(RepositorySnapshot))
    snapshot.stats = {k: v for k, v in snapshot.stats.items() if k != "graph"}
    db.commit()
    response = client.post(url, json={"targets": [{"path": "shop/billing.py"}]})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "graph_not_built"
