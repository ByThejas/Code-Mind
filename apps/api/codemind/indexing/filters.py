"""Server-side rules for which repository files are indexed. `.gitignore` is not trusted for this."""

import re
from pathlib import PurePosixPath

IGNORED_DIRS = frozenset(
    {
        ".git", "node_modules", "dist", "build", "out", "vendor", ".venv", "venv",
        "__pycache__", ".next", "coverage", "target", ".cache", ".idea", ".vscode",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".gradle",
    }
)  # fmt: skip

GENERATED_NAMES = frozenset(
    {
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
        "uv.lock", "Cargo.lock", "composer.lock", "Gemfile.lock", "go.sum",
    }
)  # fmt: skip
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", ".pyc", ".pyo")

SECRET_NAMES = frozenset(
    {".npmrc", ".pypirc", ".netrc", ".htpasswd", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
)
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx")
_SECRET_NAME_PATTERNS = (
    re.compile(r"^\.env(\..+)?$"),
    re.compile(r"^credentials.*\.json$", re.IGNORECASE),
    re.compile(r"^secrets?\..+$", re.IGNORECASE),
    re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(\..+)?$"),
)
_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})

# Content patterns for likely credentials. A detector reduces risk; it does not guarantee detection.
_SECRET_CONTENT = re.compile(
    rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"
    rb"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    rb"|\bgh[pousr]_[A-Za-z0-9]{36,}\b"
    rb"|\bgithub_pat_[A-Za-z0-9_]{60,}\b"
    rb"|\bxox[abpr]-[A-Za-z0-9-]{10,}\b"
    rb"|\bsk-[A-Za-z0-9_-]{32,}\b"
    rb"|(?i:\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|password)\b)\s*[:=]\s*"
    rb"['\"][^'\"\s$<{]{16,}['\"]"
)

LANGUAGES_BY_SUFFIX = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".java": "java", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".kt": "kotlin", ".swift": "swift", ".scala": "scala", ".sh": "shell", ".bash": "shell",
    ".sql": "sql", ".html": "html", ".css": "css", ".scss": "scss", ".vue": "vue",
    ".svelte": "svelte", ".md": "markdown", ".rst": "rst", ".txt": "text",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini",
    ".cfg": "ini", ".xml": "xml",
}  # fmt: skip
LANGUAGES_BY_NAME = {
    "Dockerfile": "dockerfile", "Makefile": "makefile", ".gitignore": "text",
    ".env.example": "text", "README": "text", "LICENSE": "text",
}  # fmt: skip


def path_exclusion(path: str) -> str | None:
    """Return why `path` must not be indexed, judged on the path alone, or None if it may be."""
    parts = PurePosixPath(path).parts
    if any(part in IGNORED_DIRS for part in parts[:-1]):
        return "ignored_path"
    name = parts[-1]
    if name in _ENV_TEMPLATES:
        return None
    lower = name.lower()
    if (
        name in SECRET_NAMES
        or lower.endswith(SECRET_SUFFIXES)
        or any(p.match(name) for p in _SECRET_NAME_PATTERNS)
    ):
        return "secret_path"
    if name in GENERATED_NAMES or lower.endswith(GENERATED_SUFFIXES):
        return "generated"
    return None


def content_exclusion(data: bytes) -> str | None:
    """Return why file content must not be indexed (binary or likely secret), or None."""
    if b"\x00" in data[:8192]:
        return "binary"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    if _SECRET_CONTENT.search(data):
        return "secret_content"
    return None


def detect_language(path: str) -> str | None:
    name = PurePosixPath(path).name
    if name in LANGUAGES_BY_NAME:
        return LANGUAGES_BY_NAME[name]
    return LANGUAGES_BY_SUFFIX.get(PurePosixPath(name).suffix.lower())
