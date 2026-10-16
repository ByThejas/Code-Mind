"""GitHub access for public repositories: URL parsing, ref resolution and tarball download.

User input never becomes a hostname: requests only go to the configured API host, and redirects are
followed only to GitHub's own download host.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Self
from urllib.parse import quote, urlsplit

import httpx

from codemind.core.config import Settings
from codemind.core.errors import CodemindError

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_DOWNLOAD_HOSTS = {"api.github.com", "codeload.github.com"}


@dataclass(frozen=True)
class RepositoryRef:
    owner: str
    name: str
    ref: str | None = None


@dataclass(frozen=True)
class RepositoryMetadata:
    owner: str
    name: str
    default_branch: str
    private: bool
    size_kb: int


def parse_repository_url(value: str) -> RepositoryRef:
    """Parse `owner/repo`, `github.com/owner/repo` or `https://github.com/owner/repo[.git][/tree/ref]`."""
    text = value.strip()
    if "://" not in text and not text.lower().startswith("github.com/"):
        parts = text.split("/")
        if len(parts) != 2:
            raise _invalid_url()
        return _ref(parts[0], parts[1], None)

    parsed = urlsplit(text if "://" in text else f"https://{text}")
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in {"github.com", "www.github.com"}
        or parsed.port is not None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise _invalid_url()

    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        raise _invalid_url()
    owner, name = segments[0], segments[1]
    name = name.removesuffix(".git")
    rest = segments[2:]
    ref = None
    if rest:
        if rest[0] != "tree" or len(rest) < 2:
            raise _invalid_url()
        ref = "/".join(rest[1:])
    return _ref(owner, name, ref)


def _ref(owner: str, name: str, ref: str | None) -> RepositoryRef:
    if not _NAME.match(owner) or not _NAME.match(name) or {owner, name} & {".", ".."}:
        raise _invalid_url()
    if ref is not None and (not ref or ".." in ref or len(ref) > 255):
        raise _invalid_url()
    return RepositoryRef(owner=owner, name=name, ref=ref)


def _invalid_url() -> CodemindError:
    return CodemindError(
        "invalid_repository_url",
        "Use a public GitHub repository, like https://github.com/owner/repo or owner/repo.",
        status_code=422,
    )


class GitHubClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codemind-indexer",
        }
        if settings.github_token is not None:
            headers["Authorization"] = f"Bearer {settings.github_token.get_secret_value()}"
        self._max_bytes = settings.max_index_bytes
        self._client = httpx.Client(
            base_url=settings.github_api_url,
            headers=headers,
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_repository(self, owner: str, name: str) -> RepositoryMetadata:
        response = self._get(f"/repos/{quote(owner)}/{quote(name)}")
        if response.status_code == 404:
            raise CodemindError(
                "repository_not_found",
                f"{owner}/{name} was not found, or it is private.",
                status_code=404,
            )
        self._raise_for_status(response)
        data = response.json()
        return RepositoryMetadata(
            owner=data["owner"]["login"],
            name=data["name"],
            default_branch=data["default_branch"],
            private=bool(data["private"]),
            size_kb=int(data.get("size", 0)),
        )

    def resolve_ref(self, owner: str, name: str, ref: str) -> str:
        """Return the full commit SHA that `ref` (branch, tag or SHA) points to right now."""
        response = self._get(f"/repos/{quote(owner)}/{quote(name)}/commits/{quote(ref, safe='')}")
        if response.status_code in (404, 422):
            raise CodemindError(
                "ref_not_found", f"No branch, tag or commit named {ref!r}.", status_code=404
            )
        self._raise_for_status(response)
        sha = response.json()["sha"]
        if not _SHA.match(sha):
            raise CodemindError("fetch_failed", "GitHub returned an unexpected commit id.")
        return sha

    def download_tarball(self, owner: str, name: str, sha: str, destination: Path) -> int:
        """Stream the tarball for `sha` into `destination`, enforcing the byte limit. Returns bytes."""
        if not _SHA.match(sha):
            raise ValueError("download_tarball needs a full commit SHA")
        url: str = f"/repos/{quote(owner)}/{quote(name)}/tarball/{sha}"
        for _ in range(3):
            with self._stream(url) as response:
                if response.is_redirect:
                    url = _checked_redirect(response.headers.get("location", ""))
                    continue
                self._raise_for_status(response)
                written = 0
                with destination.open("wb") as fh:
                    for chunk in response.iter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > self._max_bytes:
                            raise CodemindError(
                                "repository_too_large",
                                "The repository archive is larger than the indexing limit.",
                            )
                        fh.write(chunk)
                return written
        raise CodemindError("fetch_failed", "Too many redirects while downloading the repository.")

    def _get(self, url: str) -> httpx.Response:
        try:
            return self._client.get(url)
        except httpx.TransportError as exc:
            raise CodemindError("fetch_failed", "Could not reach GitHub.", retryable=True) from exc

    @contextmanager
    def _stream(self, url: str) -> Iterator[httpx.Response]:
        try:
            with self._client.stream("GET", url) as response:
                yield response
        except httpx.TransportError as exc:
            raise CodemindError("fetch_failed", "Could not reach GitHub.", retryable=True) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code == 429 or (
            response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise CodemindError(
                "github_rate_limited",
                "GitHub's API rate limit was reached. Try again later or set GITHUB_TOKEN.",
                status_code=503,
                retryable=True,
            )
        raise CodemindError(
            "fetch_failed",
            f"GitHub responded with HTTP {response.status_code}.",
            status_code=502,
            retryable=response.status_code >= 500,
        )


def _checked_redirect(location: str) -> str:
    parsed = urlsplit(location)
    if parsed.scheme != "https" or (parsed.hostname or "") not in _ALLOWED_DOWNLOAD_HOSTS:
        raise CodemindError("fetch_failed", "GitHub redirected to an unexpected host.")
    return location
