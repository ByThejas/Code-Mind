from pathlib import Path

import httpx
import pytest
import respx

from codemind.core.config import Settings
from codemind.core.errors import CodemindError
from codemind.repositories.github import GitHubClient

API = "https://api.github.com"
SHA = "c" * 40


@pytest.fixture
def client() -> GitHubClient:
    return GitHubClient(Settings(max_index_bytes=1000))


@respx.mock
def test_resolve_ref_and_metadata(client: GitHubClient) -> None:
    respx.get(f"{API}/repos/o/r").respond(
        json={
            "owner": {"login": "O"},
            "name": "R",
            "default_branch": "main",
            "private": False,
            "size": 3,
        }
    )
    respx.get(f"{API}/repos/o/r/commits/feature%2Fx").respond(json={"sha": SHA})
    meta = client.get_repository("o", "r")
    assert (meta.owner, meta.name, meta.default_branch) == ("O", "R", "main")
    assert client.resolve_ref("o", "r", "feature/x") == SHA


@respx.mock
def test_errors_map_to_codes(client: GitHubClient) -> None:
    respx.get(f"{API}/repos/o/missing").respond(404)
    respx.get(f"{API}/repos/o/r/commits/nope").respond(422)
    respx.get(f"{API}/repos/o/limited").respond(403, headers={"x-ratelimit-remaining": "0"})
    respx.get(f"{API}/repos/o/down").respond(503)
    cases = [
        (lambda: client.get_repository("o", "missing"), "repository_not_found", False),
        (lambda: client.resolve_ref("o", "r", "nope"), "ref_not_found", False),
        (lambda: client.get_repository("o", "limited"), "github_rate_limited", True),
        (lambda: client.get_repository("o", "down"), "fetch_failed", True),
    ]
    for call, code, retryable in cases:
        with pytest.raises(CodemindError) as err:
            call()
        assert (err.value.code, err.value.retryable) == (code, retryable)


@respx.mock
def test_tarball_follows_only_github_redirects(client: GitHubClient, tmp_path: Path) -> None:
    respx.get(f"{API}/repos/o/r/tarball/{SHA}").respond(
        302, headers={"location": f"https://codeload.github.com/o/r/legacy.tar.gz/{SHA}"}
    )
    respx.get(f"https://codeload.github.com/o/r/legacy.tar.gz/{SHA}").respond(content=b"abc")
    assert client.download_tarball("o", "r", SHA, tmp_path / "t.tgz") == 3

    respx.get(f"{API}/repos/o/evil/tarball/{SHA}").respond(
        302, headers={"location": "http://169.254.169.254/latest/meta-data"}
    )
    with pytest.raises(CodemindError, match="unexpected host"):
        client.download_tarball("o", "evil", SHA, tmp_path / "e.tgz")


@respx.mock
def test_tarball_size_limit(client: GitHubClient, tmp_path: Path) -> None:
    respx.get(f"{API}/repos/o/r/tarball/{SHA}").respond(content=b"x" * 5000)
    with pytest.raises(CodemindError) as err:
        client.download_tarball("o", "r", SHA, tmp_path / "t.tgz")
    assert err.value.code == "repository_too_large"


def test_token_is_sent_when_configured() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"sha": SHA})

    transport = httpx.MockTransport(handler)
    GitHubClient(Settings(github_token="t0ken"), transport=transport).resolve_ref("o", "r", "main")
    GitHubClient(Settings(github_token=None), transport=transport).resolve_ref("o", "r", "main")
    assert seen == ["Bearer t0ken", None]
