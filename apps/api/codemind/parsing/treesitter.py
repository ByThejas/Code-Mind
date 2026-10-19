"""Tree-sitter parsing for the first supported languages (Python, TS/TSX, JS).

One parse yields the file's symbols plus the raw material for the knowledge graph: its imports and
the calls and base classes it names. Names are not resolved here; graph/build.py does that once every
file in the snapshot is known.
"""

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
class ParsedImport:
    """One import statement (or `require`, re-export, literal `import()`)."""

    module: str  # "os.path", "./store", "" for Python `from . import x`
    line: int
    level: int = 0  # leading dots of a Python relative import
    # (imported name, local name). Imported "default" is a JS default import.
    bindings: list[tuple[str, str]] = field(default_factory=list)
    # Local name bound to the module itself: `import a.b as c`, `import * as ns`, `x = require()`.
    module_alias: str | None = None
    dynamic: bool = False  # literal `import("./x")`: resolvable, but only loaded at runtime


@dataclass
class ParsedReference:
    """A use of a name that may point at a symbol: a call or a base class."""

    relation: str  # "calls" | "extends" | "implements"
    name: str
    receiver: str | None  # "self", "this", "pkg.mod", "obj"... None for a bare name
    line: int
    source: ParsedSymbol | None  # innermost enclosing symbol, None at module level


@dataclass
class ParseResult:
    symbols: list[ParsedSymbol]  # every symbol, in source order
    has_errors: bool
    imports: list[ParsedImport] = field(default_factory=list)
    references: list[ParsedReference] = field(default_factory=list)
    # Imports whose target is computed at runtime (importlib, __import__, require(expr), import(expr)).
    dynamic_imports: int = 0


MAX_REFERENCES_PER_FILE = 5000


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
    result = ParseResult(symbols=symbols, has_errors=tree.root_node.has_error)
    if not result.has_errors:
        _References(language, symbols, result).visit(tree.root_node)
    return result


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


# --- imports and references -------------------------------------------------------------------

_DYNAMIC_PY_IMPORTS = frozenset({"import_module", "__import__"})


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text else ""


def _dotted(node: Node | None) -> str | None:
    """`a`, `a.b.c`, `self`, `this.x`: a plain name chain, or None for anything more complex."""
    if node is None:
        return None
    if node.type in ("identifier", "this", "property_identifier", "type_identifier"):
        return _text(node)
    if node.type in ("attribute", "member_expression"):
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute") or node.child_by_field_name("property")
        base = _dotted(obj)
        return f"{base}.{_text(attr)}" if base and attr is not None else None
    return None


def _string_value(node: Node | None) -> str | None:
    if node is None or node.type != "string":
        return None
    fragments = [c for c in node.children if c.type == "string_fragment"]
    return _text(fragments[0]) if len(fragments) == 1 else ("" if not fragments else None)


class _References:
    def __init__(self, language: str, symbols: list[ParsedSymbol], out: ParseResult) -> None:
        self.python = language == "python"
        self.out = out
        # innermost symbol per line: parents come first in source order, children overwrite them
        self.enclosing: dict[int, ParsedSymbol] = {}
        for symbol in symbols:
            for line in range(symbol.start_line, symbol.end_line + 1):
                self.enclosing[line] = symbol

    def visit(self, root: Node) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            if len(self.out.references) >= MAX_REFERENCES_PER_FILE:
                return
            self._node(node)
            stack.extend(reversed(node.children))

    def _line(self, node: Node) -> int:
        return node.start_point[0] + 1

    def _ref(
        self, relation: str, target: Node | None, at: Node, source: ParsedSymbol | None = None
    ) -> None:
        name = _dotted(target)
        if not name:
            return
        receiver, _, last = name.rpartition(".")
        line = self._line(at)
        self.out.references.append(
            ParsedReference(
                relation, last, receiver or None, line, source or self.enclosing.get(line)
            )
        )

    def _class_symbol(self, class_node: Node | None) -> ParsedSymbol | None:
        """The symbol for a class node, so its base classes are attributed to the class itself."""
        if class_node is None:
            return None
        name = _text(class_node.child_by_field_name("name"))
        line = self._line(class_node)
        symbol = self.enclosing.get(line)
        while symbol is not None and not (symbol.kind == "class" and symbol.name == name):
            symbol = symbol.parent
        return symbol

    def _node(self, node: Node) -> None:
        if self.python:
            self._python(node)
        else:
            self._javascript(node)

    # Python

    def _python(self, node: Node) -> None:
        line = self._line(node)
        if node.type == "import_statement":
            for child in node.children_by_field_name("name"):
                if child.type == "aliased_import":
                    module = _text(child.child_by_field_name("name"))
                    alias = _text(child.child_by_field_name("alias"))
                else:
                    module = alias = _text(child)
                self.out.imports.append(ParsedImport(module, line, module_alias=alias))
        elif node.type == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            level, module = 0, _text(module_node)
            if module_node is not None and module_node.type == "relative_import":
                prefix = next((c for c in module_node.children if c.type == "import_prefix"), None)
                level = len(_text(prefix))
                dotted = next((c for c in module_node.children if c.type == "dotted_name"), None)
                module = _text(dotted)
            bindings = []
            for child in node.children_by_field_name("name"):
                if child.type == "aliased_import":
                    name = _text(child.child_by_field_name("name"))
                    bindings.append((name, _text(child.child_by_field_name("alias"))))
                else:
                    bindings.append((_text(child), _text(child)))
            self.out.imports.append(ParsedImport(module, line, level=level, bindings=bindings))
        elif node.type == "call":
            function = node.child_by_field_name("function")
            callee = _dotted(function)
            if callee and callee.rpartition(".")[2] in _DYNAMIC_PY_IMPORTS:
                self.out.dynamic_imports += 1
            else:
                self._ref("calls", function, node)
        elif node.type == "class_definition":
            bases = node.child_by_field_name("superclasses")
            owner = self._class_symbol(node)
            for base in bases.children if bases is not None else []:
                if base.type in ("identifier", "attribute"):
                    self._ref("extends", base, base, owner)

    # JavaScript / TypeScript

    def _javascript(self, node: Node) -> None:
        line = self._line(node)
        if node.type in ("import_statement", "export_statement"):
            module = _string_value(node.child_by_field_name("source"))
            if module is None:
                return
            imp = ParsedImport(module, line)
            clause = next((c for c in node.children if c.type == "import_clause"), None)
            for part in clause.children if clause is not None else []:
                if part.type == "identifier":
                    imp.bindings.append(("default", _text(part)))
                elif part.type == "namespace_import":
                    ns = next((c for c in part.children if c.type == "identifier"), None)
                    imp.module_alias = _text(ns) or None
                elif part.type == "named_imports":
                    for spec in part.children:
                        if spec.type == "import_specifier":
                            imported = _text(spec.child_by_field_name("name"))
                            local = _text(spec.child_by_field_name("alias")) or imported
                            imp.bindings.append((imported, local))
            self.out.imports.append(imp)
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if (
                function is not None
                and function.type in ("import", "identifier")
                and (function.type == "import" or _text(function) == "require")
            ):
                first = next((a for a in args.named_children), None) if args else None
                module = _string_value(first)
                if module is None:
                    self.out.dynamic_imports += 1
                    return
                imp = ParsedImport(module, line, dynamic=function.type == "import")
                self._require_target(node, imp)
                self.out.imports.append(imp)
            else:
                self._ref("calls", function, node)
        elif node.type == "new_expression":
            self._ref("calls", node.child_by_field_name("constructor"), node)
        elif node.type in ("extends_clause", "implements_clause"):
            heritage = node.parent
            owner = self._class_symbol(heritage.parent if heritage is not None else None)
            if node.type == "extends_clause":
                self._ref("extends", node.child_by_field_name("value"), node, owner)
            else:
                for child in node.named_children:
                    self._ref("implements", child, child, owner)

    def _require_target(self, call: Node, imp: ParsedImport) -> None:
        """`const x = require("m")` binds x to the module; `const {a, b: c} = ...` binds names."""
        declarator = call.parent
        if declarator is None or declarator.type != "variable_declarator":
            return
        target = declarator.child_by_field_name("name")
        if target is None:
            return
        if target.type == "identifier":
            imp.module_alias = _text(target)
        elif target.type == "object_pattern":
            for part in target.named_children:
                if part.type == "shorthand_property_identifier_pattern":
                    imp.bindings.append((_text(part), _text(part)))
                elif part.type == "pair_pattern":
                    key = _text(part.child_by_field_name("key"))
                    value = _text(part.child_by_field_name("value"))
                    if key and value:
                        imp.bindings.append((key, value))
