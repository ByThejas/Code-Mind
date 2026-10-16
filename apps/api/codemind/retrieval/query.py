"""Turn a question into keyword terms, code identifiers and file paths.

Terms are reduced to [a-z0-9]+ before they reach to_tsquery, so user input never becomes tsquery syntax.
"""

import re
from dataclasses import dataclass

MAX_TERMS = 32
MAX_IDENTIFIERS = 10

STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "being",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "each",
        "else",
        "for",
        "from",
        "get",
        "gets",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "me",
        "more",
        "most",
        "my",
        "no",
        "not",
        "now",
        "of",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "same",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "code",
        "codebase",
        "repo",
        "repository",
        "file",
        "files",
        "function",
        "functions",
        "work",
        "works",
        "explain",
        "show",
        "tell",
        "find",
        "please",
        "happen",
        "happens",
        "used",
        "use",
        "uses",
    }
)

_WORD = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"[a-z][A-Z]|[A-Z]{2}[a-z]")
_PATH_EXT = re.compile(r"\.[A-Za-z0-9]{1,6}$")
_CODE_EXTS = frozenset(
    {
        "py",
        "ts",
        "tsx",
        "js",
        "jsx",
        "mjs",
        "cjs",
        "md",
        "json",
        "toml",
        "yaml",
        "yml",
        "ini",
        "cfg",
        "txt",
        "sql",
        "sh",
        "go",
        "rs",
        "java",
        "rb",
        "css",
        "html",
    }
)


@dataclass(frozen=True)
class ParsedQuery:
    terms: list[str]
    identifiers: list[str]
    paths: list[str]

    @property
    def tsquery(self) -> str | None:
        """Any-term query; ts_rank_cd then ranks chunks with more of the terms higher."""
        return " | ".join(self.terms) if self.terms else None


def parse_query(question: str) -> ParsedQuery:
    terms: list[str] = []
    for word in _WORD.findall(question.lower()):
        if (len(word) > 1 or word.isdigit()) and word not in STOPWORDS and word not in terms:
            terms.append(word)

    identifiers: list[str] = []
    paths: list[str] = []
    for raw in re.findall(r"[\w./()`-]+", question):
        token = raw.strip(".,;:!?()'\"`").removesuffix("()")
        if not token:
            continue
        ext = _PATH_EXT.search(token)
        if ext and ext.group()[1:].lower() in _CODE_EXTS:
            if token not in paths:
                paths.append(token)
        elif (
            re.fullmatch(r"[A-Za-z_][\w.]*", token)
            and _looks_like_code(raw, token)
            and token not in identifiers
        ):
            identifiers.append(token)
    return ParsedQuery(terms[:MAX_TERMS], identifiers[:MAX_IDENTIFIERS], paths[:MAX_IDENTIFIERS])


def _looks_like_code(raw: str, token: str) -> bool:
    return (
        "_" in token
        or "." in token.strip(".")
        or bool(_CAMEL.search(token))
        or raw.endswith("()")
        or raw.startswith("`")
    )
