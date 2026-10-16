import pytest

from codemind.indexing.filters import content_exclusion, detect_language, path_exclusion


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("src/app.py", None),
        (".env.example", None),
        ("docs/node_modules.md", None),
        ("node_modules/x/index.js", "ignored_path"),
        ("pkg/__pycache__/a.pyc", "ignored_path"),
        ("a/.git/config", "ignored_path"),
        (".env", "secret_path"),
        ("deploy/.env.production", "secret_path"),
        ("keys/server.pem", "secret_path"),
        ("home/id_rsa", "secret_path"),
        ("credentials-prod.json", "secret_path"),
        ("config/secrets.yaml", "secret_path"),
        ("package-lock.json", "generated"),
        ("static/app.min.js", "generated"),
        ("static/app.js.map", "generated"),
    ],
)
def test_path_exclusion(path: str, reason: str | None) -> None:
    assert path_exclusion(path) == reason


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"print('hi')\n", None),
        (b"password = os.environ['DB_PASSWORD']\n", None),
        (b"abc\x00def", "binary"),
        (b"\xff\xfe\xfa", "binary"),
        (b"key = 'AKIAABCDEFGHIJKLMNOP'", "secret_content"),
        (b"-----BEGIN OPENSSH PRIVATE KEY-----\n", "secret_content"),
        (b"token: ghp_" + b"a" * 36, "secret_content"),
        (b'API_KEY = "sk_live_abcdefghijklmnop1234"', "secret_content"),
    ],
)
def test_content_exclusion(data: bytes, reason: str | None) -> None:
    assert content_exclusion(data) == reason


def test_detect_language() -> None:
    assert detect_language("a/b.py") == "python"
    assert detect_language("c.tsx") == "tsx"
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language("x.unknown") is None
