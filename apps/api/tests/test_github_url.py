import pytest

from codemind.core.errors import CodemindError
from codemind.repositories.github import RepositoryRef, parse_repository_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("pallets/flask", RepositoryRef("pallets", "flask")),
        ("https://github.com/pallets/flask", RepositoryRef("pallets", "flask")),
        ("https://github.com/pallets/flask.git", RepositoryRef("pallets", "flask")),
        ("https://github.com/pallets/flask/", RepositoryRef("pallets", "flask")),
        ("github.com/pallets/flask", RepositoryRef("pallets", "flask")),
        ("https://www.github.com/pallets/flask", RepositoryRef("pallets", "flask")),
        (
            "https://github.com/pallets/flask/tree/release/3.0",
            RepositoryRef("pallets", "flask", "release/3.0"),
        ),
    ],
)
def test_accepts_github_forms(value: str, expected: RepositoryRef) -> None:
    assert parse_repository_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "flask",
        "a/b/c",
        "http://github.com/pallets/flask",
        "https://github.com.evil.com/pallets/flask",
        "https://evil.com/github.com/pallets/flask",
        "https://user:pw@github.com/pallets/flask",
        "https://github.com:8443/pallets/flask",
        "https://github.com/pallets",
        "https://github.com/pallets/flask/blob/main/x.py",
        "https://github.com/pallets/flask?x=1",
        "https://github.com/../flask",
        "https://github.com/pallets/flask/tree/../../x",
        "file:///etc/passwd",
        "https://127.0.0.1/pallets/flask",
        "own er/repo",
    ],
)
def test_rejects_other_urls(value: str) -> None:
    with pytest.raises(CodemindError) as err:
        parse_repository_url(value)
    assert err.value.code == "invalid_repository_url"
