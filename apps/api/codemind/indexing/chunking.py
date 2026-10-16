"""Turn a file into retrieval chunks with exact line provenance.

Top-level symbols become one chunk each (large ones are split into overlapping windows that keep the
symbol name); code between symbols and files without a parser get plain line windows.
"""

from dataclasses import dataclass

from codemind.parsing.treesitter import ParsedSymbol

WINDOW_LINES = 60
WINDOW_OVERLAP = 10
MAX_SYMBOL_LINES = 120


@dataclass(frozen=True)
class Chunk:
    start_line: int  # 1-based, inclusive
    end_line: int
    kind: str  # "symbol" | "lines"
    symbol_name: str | None
    text: str

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.text) // 4)


def line_windows(
    lines: list[str], start: int, end: int, symbol_name: str | None = None
) -> list[Chunk]:
    """Chunks covering lines `start..end` (1-based, inclusive) in overlapping windows."""
    chunks: list[Chunk] = []
    if end < start:
        return chunks
    kind = "symbol" if symbol_name else "lines"
    step = WINDOW_LINES - WINDOW_OVERLAP
    first = start
    while True:
        last = min(first + WINDOW_LINES - 1, end)
        text = "".join(lines[first - 1 : last])
        if text.strip():
            chunks.append(Chunk(first, last, kind, symbol_name, text))
        if last >= end:
            return chunks
        first += step


def chunk_file(text: str, symbols: list[ParsedSymbol] | None = None) -> list[Chunk]:
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if total == 0:
        return []
    if not symbols:
        return line_windows(lines, 1, total)

    top_level = sorted(
        (s for s in symbols if s.parent is None), key=lambda s: (s.start_line, s.end_line)
    )
    chunks: list[Chunk] = []
    cursor = 1
    for symbol in top_level:
        start, end = max(symbol.start_line, cursor), min(symbol.end_line, total)
        if end < start:
            continue
        chunks.extend(line_windows(lines, cursor, start - 1))
        if end - start + 1 <= MAX_SYMBOL_LINES:
            chunks.append(
                Chunk(start, end, "symbol", symbol.qualified_name, "".join(lines[start - 1 : end]))
            )
        else:
            chunks.extend(line_windows(lines, start, end, symbol.qualified_name))
        cursor = end + 1
    chunks.extend(line_windows(lines, cursor, total))
    return chunks
