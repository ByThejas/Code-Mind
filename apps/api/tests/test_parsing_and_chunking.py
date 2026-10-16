from codemind.indexing.chunking import MAX_SYMBOL_LINES, WINDOW_LINES, chunk_file
from codemind.parsing.treesitter import parse_symbols
from tests.fixtures import APP_PY, INDEX_TS


def test_python_symbols() -> None:
    result = parse_symbols("python", APP_PY.decode())
    assert not result.has_errors
    assert [(s.qualified_name, s.kind, s.start_line, s.end_line) for s in result.symbols] == [
        ("greet", "function", 4, 6),
        ("Greeter", "class", 9, 14),
        ("Greeter.__init__", "method", 10, 11),
        ("Greeter.greet", "method", 13, 14),
    ]


def test_typescript_symbols() -> None:
    result = parse_symbols("typescript", INDEX_TS.decode())
    assert [(s.qualified_name, s.kind) for s in result.symbols] == [
        ("User", "interface"),
        ("add", "function"),
        ("Store", "class"),
        ("Store.get", "method"),
    ]


def test_syntax_error_is_reported() -> None:
    assert parse_symbols("python", "def oops(:\n").has_errors


def test_symbol_chunks_cover_the_file() -> None:
    text = APP_PY.decode()
    chunks = chunk_file(text, parse_symbols("python", text).symbols)
    assert [(c.kind, c.symbol_name, c.start_line, c.end_line) for c in chunks] == [
        ("lines", None, 1, 3),
        ("symbol", "greet", 4, 6),
        ("symbol", "Greeter", 9, 14),  # blank lines 7-8 make no chunk
    ]
    lines = text.splitlines(keepends=True)
    for c in chunks:
        assert c.text == "".join(lines[c.start_line - 1 : c.end_line])


def test_line_windows_overlap_and_end_exactly() -> None:
    text = "".join(f"line {i}\n" for i in range(1, 131))
    chunks = chunk_file(text)
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 60), (51, 110), (101, 130)]
    assert all(c.kind == "lines" for c in chunks)


def test_large_symbol_is_split_with_its_name() -> None:
    body = "".join(f"    x{i} = {i}\n" for i in range(MAX_SYMBOL_LINES + 20))
    text = "def big():\n" + body
    chunks = chunk_file(text, parse_symbols("python", text).symbols)
    assert len(chunks) > 1
    assert all(c.symbol_name == "big" and c.kind == "symbol" for c in chunks)
    assert chunks[0].end_line - chunks[0].start_line + 1 == WINDOW_LINES
    assert chunks[-1].end_line == text.count("\n")


def test_empty_file_has_no_chunks() -> None:
    assert chunk_file("") == []
