"""Symbol extraction with Tree-sitter for the first supported languages (Python, TS/TSX, JS)."""

from dataclasses import dataclass, field
from functools import cache

import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

SUPPORTED_LANGUAGES = frozenset({"python", "typescript", "tsx", "javascript"})

# node type -> symbol kind
_SYMBOL_NODES = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "javascript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
    },
}
_SYMBOL_NODES["typescript"] = _SYMBOL_NODES["javascript"] | {
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "abstract_class_declaration": "class",
}
_SYMBOL_NODES["tsx"] = _SYMBOL_NODES["typescript"]
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})


@dataclass
class ParsedSymbol:
    name: str
    qualified_name: str
    kind: str
    start_line: int  # 1-based, inclusive
    end_line: int
    parent: "ParsedSymbol | None" = None
    children: list["ParsedSymbol"] = field(default_factory=list)


@dataclass
class ParseResult:
    symbols: list[ParsedSymbol]  # every symbol, in source order
    has_errors: bool


@cache
def _parser(language: str) -> Parser:
    grammar = {
        "python": tree_sitter_python.language,
        "javascript": tree_sitter_javascript.language,
        "typescript": tree_sitter_typescript.language_typescript,
        "tsx": tree_sitter_typescript.language_tsx,
    }[language]
    return Parser(Language(grammar()))


def parse_symbols(language: str, source: str) -> ParseResult:
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"unsupported language: {language}")
    tree = _parser(language).parse(source.encode("utf-8"))
    symbols: list[ParsedSymbol] = []
    _walk(tree.root_node, language, None, symbols, depth=0)
    return ParseResult(symbols=symbols, has_errors=tree.root_node.has_error)


def _walk(
    node: Node, language: str, parent: ParsedSymbol | None, out: list[ParsedSymbol], depth: int
) -> None:
    if depth > 64:
        return
    for child in node.children:
        span_node = child
        target = child
        if child.type == "decorated_definition":
            target = child.child_by_field_name("definition") or child
        kind = _SYMBOL_NODES[language].get(target.type)
        name_node = target.child_by_field_name("name") if kind else None

        if kind is None and target.type == "variable_declarator":
            value = target.child_by_field_name("value")
            if value is not None and value.type in _FUNCTION_VALUES:
                kind, name_node = "function", target.child_by_field_name("name")
                # Span the whole `const x = () => {}` statement where possible.
                span_node = (
                    node if node.type in ("lexical_declaration", "variable_declaration") else target
                )

        if kind and name_node is not None and name_node.text:
            if kind == "function" and parent is not None and parent.kind == "class":
                kind = "method"
            name = name_node.text.decode("utf-8", "replace")
            symbol = ParsedSymbol(
                name=name,
                qualified_name=f"{parent.qualified_name}.{name}" if parent else name,
                kind=kind,
                start_line=span_node.start_point[0] + 1,
                end_line=span_node.end_point[0] + 1,
                parent=parent,
            )
            out.append(symbol)
            if parent is not None:
                parent.children.append(symbol)
            _walk(target, language, symbol, out, depth + 1)
        else:
            _walk(child, language, parent, out, depth + 1)
