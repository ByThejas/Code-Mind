"""Resolve parsed imports, calls and base classes into graph edges for one snapshot.

Runs once every file of the snapshot is parsed, because a name can only be resolved when the file
that defines it is known. Each edge records how it was resolved:

- "static": the code names its target directly (an import path that exists, a call to a name that
  was imported or defined in scope, `self.method()` on the enclosing class).
- "inferred": a best guess (several files could match an import, a method found on a base class, a
  name that matches exactly one definition in the repository). Confidence says how good the guess is.

Anything else (calls into libraries, dynamic imports, attribute calls on objects of unknown type
with a common method name) is left out and counted, so the API can say what the graph cannot see.
See docs/graph-design.md.
"""

import posixpath
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from codemind.parsing.treesitter import ParsedImport

PYTHON = "python"
JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
SELF_NAMES = frozenset({"self", "cls", "this"})
ALIAS_PREFIXES = ("@/", "~/", "#")
# Imports of non-code files (styles, images, data) are not graph edges.
ASSET_EXTENSIONS = frozenset(
    {".css", ".scss", ".sass", ".less", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}
    | {".json", ".html", ".md", ".wasm", ".txt", ".woff", ".woff2"}
)
SAMPLE_SIZE = 20
MAX_BASE_DEPTH = 3

# Method names too common to guess a target from when the receiver's type is unknown.
COMMON_METHODS = frozenset(
    [
        "get",
        "set",
        "add",
        "update",
        "items",
        "keys",
        "values",
        "push",
        "pop",
        "map",
        "filter",
        "join",
        "split",
        "format",
        "then",
        "catch",
        "send",
        "read",
        "write",
        "close",
        "open",
        "run",
        "start",
        "stop",
        "emit",
        "call",
        "apply",
        "bind",
        "toString",
        "forEach",
        "reduce",
        "find",
        "includes",
        "replace",
        "strip",
        "lower",
        "upper",
        "encode",
        "decode",
        "load",
        "dump",
        "loads",
        "dumps",
        "copy",
        "clear",
        "remove",
        "insert",
        "extend",
        "sort",
        "index",
        "count",
        "exists",
        "query",
        "execute",
        "commit",
        "rollback",
        "flush",
        "render",
        "log",
        "info",
        "debug",
        "warn",
        "warning",
        "error",
        "exception",
        "append",
        "delete",
        "post",
        "patch",
        "put",
        "fetch",
        "next",
        "json",
        "text",
        "parse",
        "create",
        "save",
        "list",
    ]
)

CONFIDENCE = {
    "import": 1.0,  # relative import that names an existing file
    "import-module": 0.9,  # absolute import matched to one file
    "import-ambiguous": 0.6,
    "local": 0.95,
    "imported-name": 0.9,
    "module-attribute": 0.9,
    "self-method": 0.9,
    "class-attribute": 0.85,
    "re-export": 0.75,
    "inherited-method": 0.7,
    "name-match": 0.4,
    "method-name-match": 0.3,
}
STATIC_HOW = frozenset(
    {"import", "import-module", "local", "imported-name", "module-attribute", "self-method"}
)


@dataclass(frozen=True, slots=True)
class SymbolNode:
    id: uuid.UUID
    file_id: uuid.UUID
    name: str
    kind: str
    parent_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class Reference:
    relation: str  # "calls" | "extends" | "implements"
    name: str
    receiver: str | None
    line: int
    source_id: uuid.UUID | None  # enclosing symbol


@dataclass
class FileGraphInput:
    file_id: uuid.UUID
    path: str
    language: str
    symbols: list[SymbolNode]
    imports: list[ParsedImport]
    references: list[Reference]
    dynamic_imports: int = 0


@dataclass
class GraphBuild:
    edges: list[dict[str, Any]]
    stats: dict[str, Any]
    warnings: list[dict[str, Any]]


@dataclass
class _Scope:
    """What a file's imports bind: names to (file, imported name) and aliases to whole modules."""

    names: dict[str, tuple["FileGraphInput | None", str]] = field(default_factory=dict)
    modules: dict[str, "FileGraphInput | None"] = field(default_factory=dict)  # None: external


@dataclass
class _Resolved:
    target: SymbolNode
    how: str


def _family(language: str) -> str:
    return PYTHON if language == PYTHON else "js"


def build_graph(snapshot_id: uuid.UUID, files: list[FileGraphInput]) -> GraphBuild:
    return _Builder(snapshot_id, files).build()


class _Builder:
    def __init__(self, snapshot_id: uuid.UUID, files: list[FileGraphInput]) -> None:
        self.snapshot_id = snapshot_id
        self.files = files
        self.by_path = {f.path: f for f in files}
        self.symbols: dict[uuid.UUID, SymbolNode] = {}
        self.top_level: dict[uuid.UUID, dict[str, SymbolNode]] = defaultdict(dict)
        self.children: dict[uuid.UUID, dict[str, SymbolNode]] = defaultdict(dict)
        self.global_names: dict[tuple[str, str], list[SymbolNode]] = defaultdict(list)
        self.method_names: dict[tuple[str, str], list[SymbolNode]] = defaultdict(list)
        self.file_family: dict[uuid.UUID, str] = {}
        for f in files:
            family = _family(f.language)
            self.file_family[f.file_id] = family
            for s in f.symbols:
                self.symbols[s.id] = s
                if s.parent_id is None:
                    self.top_level[f.file_id].setdefault(s.name, s)
                    if s.kind in ("function", "class", "interface", "type", "enum"):
                        self.global_names[(family, s.name)].append(s)
                else:
                    self.children[s.parent_id].setdefault(s.name, s)
                    if s.kind == "method":
                        self.method_names[(family, s.name)].append(s)
        self.py_canonical, self.py_suffixes = self._python_modules()
        self.scopes: dict[uuid.UUID, _Scope] = {}
        self.bases: dict[uuid.UUID, list[SymbolNode]] = defaultdict(list)
        self.edges: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.stats: Counter[str] = Counter()
        self.unresolved_imports: list[str] = []
        self.dynamic_paths: list[str] = []

    # --- module resolution ---------------------------------------------------------------------

    def _python_modules(
        self,
    ) -> tuple[dict[str, FileGraphInput], dict[str, list[FileGraphInput]]]:
        """Canonical module names (package root = first parent without __init__.py) and suffixes."""
        package_dirs = {
            posixpath.dirname(f.path)
            for f in self.files
            if f.language == PYTHON and posixpath.basename(f.path) == "__init__.py"
        }
        canonical: dict[str, FileGraphInput] = {}
        suffixes: dict[str, list[FileGraphInput]] = defaultdict(list)
        for f in self.files:
            if f.language != PYTHON:
                continue
            directory, base = posixpath.split(f.path)
            parts = [] if base == "__init__.py" else [base.removesuffix(".py")]
            while directory and directory in package_dirs:
                directory, name = posixpath.split(directory)
                parts.insert(0, name)
            if parts:
                canonical.setdefault(".".join(parts), f)
            all_parts = [p for p in f.path.removesuffix(".py").split("/") if p]
            if base == "__init__.py":
                all_parts = all_parts[:-1]
            for i in range(len(all_parts)):
                suffixes[".".join(all_parts[i:])].append(f)
        return canonical, suffixes

    def _python_absolute(
        self, module: str, importer: FileGraphInput
    ) -> tuple[FileGraphInput | None, str]:
        if module in self.py_canonical:
            return self.py_canonical[module], "import-module"
        candidates = self.py_suffixes.get(module, []) if "." in module else []
        if len(candidates) == 1:
            return candidates[0], "import-module"
        if candidates:
            best = max(candidates, key=lambda c: _shared_prefix(c.path, importer.path))
            return best, "import-ambiguous"
        return None, "external"

    def _python_relative(self, importer: FileGraphInput, level: int, module: str) -> str | None:
        """Path stem of a relative import's target, e.g. "pkg/models" (".py" or "/__init__.py")."""
        directory = posixpath.dirname(importer.path)
        for _ in range(level - 1):
            if not directory:
                return None
            directory = posixpath.dirname(directory)
        parts = [p for p in module.split(".") if p]
        return posixpath.join(directory, *parts) if parts else directory

    def _python_file(self, stem: str) -> FileGraphInput | None:
        for candidate in (f"{stem}.py", f"{stem}/__init__.py" if stem else "__init__.py"):
            if candidate in self.by_path:
                return self.by_path[candidate]
        return None

    def _js_file(self, importer: FileGraphInput, spec: str) -> tuple[FileGraphInput | None, str]:
        if posixpath.splitext(spec)[1].lower() in ASSET_EXTENSIONS:
            return None, "asset"
        if spec.startswith("."):
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(importer.path), spec))
            if joined.startswith(".."):
                return None, "unresolved"
            found = self._js_candidates(joined)
            return (found, "import") if found else (None, "unresolved")
        if spec.startswith(ALIAS_PREFIXES):
            return None, "unresolved"
        found = self._js_candidates(spec)  # baseUrl-style "src/lib/x"
        return (found, "import-ambiguous") if found else (None, "external")

    def _js_candidates(self, stem: str) -> FileGraphInput | None:
        options = [stem] + [stem + ext for ext in JS_EXTENSIONS]
        options += [f"{stem}/index{ext}" for ext in JS_EXTENSIONS]
        root, ext = posixpath.splitext(stem)
        if ext in (".js", ".jsx", ".mjs", ".cjs"):  # TS sources imported by their compiled name
            options += [root + e for e in (".ts", ".tsx", ".mts", ".cts")]
        for option in options:
            found = self.by_path.get(option)
            if found is not None and _family(found.language) == "js":
                return found
        return None

    # --- passes --------------------------------------------------------------------------------

    def build(self) -> GraphBuild:
        for f in self.files:
            self.scopes[f.file_id] = self._imports(f)
            self.stats["dynamic_imports"] += f.dynamic_imports
            if f.dynamic_imports and len(self.dynamic_paths) < SAMPLE_SIZE:
                self.dynamic_paths.append(f.path)
        # Base classes first: `self.method()` may resolve to a method on a base class.
        for f in self.files:
            for ref in f.references:
                if ref.relation != "calls":
                    self._reference(f, ref)
        for f in self.files:
            for ref in f.references:
                if ref.relation == "calls":
                    self._reference(f, ref)
        return GraphBuild(list(self.edges.values()), self._stats(), self._warnings())

    def _imports(self, f: FileGraphInput) -> _Scope:
        scope = _Scope()
        for imp in f.imports:
            if f.language == PYTHON:
                self._python_import(f, imp, scope)
            else:
                self._js_import(f, imp, scope)
        return scope

    def _python_import(self, f: FileGraphInput, imp: ParsedImport, scope: _Scope) -> None:
        if imp.level:
            stem = self._python_relative(f, imp.level, imp.module)
            target = self._python_file(stem) if stem is not None else None
            how = "import"
        else:
            stem = None
            target, how = self._python_absolute(imp.module, f) if imp.module else (None, "external")
        if imp.module_alias:
            scope.modules[imp.module_alias] = target
            self._import_result(f, imp, target, how)
            return
        # `from pkg import name`: name may be a submodule file or a name defined in pkg.
        unresolved_package = target is None
        for name, local in imp.bindings:
            if name == "*":
                continue
            if stem is not None:
                submodule = self._python_file(posixpath.join(stem, name))
                sub_how = "import"
            else:
                submodule, sub_how = self._python_absolute(f"{imp.module}.{name}", f)
            if submodule is not None:
                scope.modules[local] = submodule
                self._import_edge(f, submodule, imp, sub_how)
                unresolved_package = False
            else:
                scope.names[local] = (target, name)
        if target is not None:
            self._import_edge(f, target, imp, how)
        elif unresolved_package:
            self._import_result(f, imp, None, how)

    def _js_import(self, f: FileGraphInput, imp: ParsedImport, scope: _Scope) -> None:
        target, how = self._js_file(f, imp.module)
        if imp.module_alias:
            scope.modules[imp.module_alias] = target
        for name, local in imp.bindings:
            scope.names[local] = (target, name)
        self._import_result(f, imp, target, how)

    def _import_result(
        self, f: FileGraphInput, imp: ParsedImport, target: FileGraphInput | None, how: str
    ) -> None:
        if target is not None:
            self._import_edge(f, target, imp, how)
        elif how == "unresolved" or (how == "import" and imp.level):
            self.stats["imports_unresolved"] += 1
            if len(self.unresolved_imports) < SAMPLE_SIZE:
                dots = "." * imp.level
                self.unresolved_imports.append(f"{f.path}:{imp.line} {dots}{imp.module}")
        elif how != "asset":
            self.stats["imports_external"] += 1

    def _import_edge(
        self, f: FileGraphInput, target: FileGraphInput, imp: ParsedImport, how: str
    ) -> None:
        if target.file_id == f.file_id:
            return
        kind = "dynamic-import" if imp.dynamic else "import"
        self._add_edge(
            "imports",
            f,
            None,
            target.file_id,
            None,
            imp.line,
            how,
            f"tree-sitter:{f.language}:{kind}:{how}",
        )

    def _reference(self, f: FileGraphInput, ref: Reference) -> None:
        counter = "calls" if ref.relation == "calls" else "inheritance"
        resolved = self._resolve(f, ref)
        if resolved is None:
            self.stats[f"{counter}_unresolved"] += 1
            return
        target = resolved.target
        if target.id == ref.source_id:
            return  # recursion adds nothing to the graph
        if ref.relation != "calls" and ref.source_id is not None:
            self.bases[ref.source_id].append(target)
        self._add_edge(
            ref.relation,
            f,
            ref.source_id,
            target.file_id,
            target.id,
            ref.line,
            resolved.how,
            f"tree-sitter:{f.language}:{ref.relation}:{resolved.how}",
        )

    def _add_edge(
        self,
        relation: str,
        f: FileGraphInput,
        source_symbol_id: uuid.UUID | None,
        target_file_id: uuid.UUID,
        target_symbol_id: uuid.UUID | None,
        line: int,
        how: str,
        provenance: str,
    ) -> None:
        key = (relation, f.file_id, source_symbol_id, target_file_id, target_symbol_id)
        confidence = CONFIDENCE[how]
        existing = self.edges.get(key)
        if existing is not None and existing["confidence"] >= confidence:
            return
        resolution = "static" if how in STATIC_HOW else "inferred"
        if existing is None:
            stat = (
                "imports"
                if relation == "imports"
                else ("calls" if relation == "calls" else "inheritance")
            )
            self.stats[f"{stat}_{resolution}"] += 1
        self.edges[key] = {
            "id": existing["id"] if existing else uuid.uuid4(),
            "snapshot_id": self.snapshot_id,
            "relation": relation,
            "source_file_id": f.file_id,
            "source_symbol_id": source_symbol_id,
            "target_file_id": target_file_id,
            "target_symbol_id": target_symbol_id,
            "line": line,
            "resolution": resolution,
            "confidence": confidence,
            "provenance": provenance,
        }

    # --- name resolution -----------------------------------------------------------------------

    def _resolve(self, f: FileGraphInput, ref: Reference) -> _Resolved | None:
        receiver = ref.receiver
        scope = self.scopes[f.file_id]
        if receiver in SELF_NAMES:
            return self._self_method(ref)
        if receiver is None:
            return self._bare(f, scope, ref.name, ref.source_id)
        if receiver in scope.modules:
            module = scope.modules[receiver]
            if module is None:
                return None  # a library
            found = self.top_level[module.file_id].get(ref.name)
            return _Resolved(found, "module-attribute") if found else None
        if "." not in receiver:
            owner = self._bare(f, scope, receiver, ref.source_id)
            if owner is not None and owner.target.kind == "class":
                method = self.children[owner.target.id].get(ref.name)
                if method is not None:
                    return _Resolved(method, "class-attribute")
                return None
            if owner is not None:
                return None
        return self._unique(self.method_names, f, ref.name, "method-name-match", methods=True)

    def _self_method(self, ref: Reference) -> _Resolved | None:
        cls = self._enclosing_class(ref.source_id)
        if cls is None:
            return None
        method = self.children[cls.id].get(ref.name)
        if method is not None:
            return _Resolved(method, "self-method")
        frontier, seen = list(self.bases.get(cls.id, [])), {cls.id}
        for _ in range(MAX_BASE_DEPTH):
            following: list[SymbolNode] = []
            for base in frontier:
                if base.id in seen:
                    continue
                seen.add(base.id)
                method = self.children[base.id].get(ref.name)
                if method is not None:
                    return _Resolved(method, "inherited-method")
                following.extend(self.bases.get(base.id, []))
            frontier = following
        return None

    def _enclosing_class(self, symbol_id: uuid.UUID | None) -> SymbolNode | None:
        while symbol_id is not None:
            symbol = self.symbols[symbol_id]
            if symbol.kind == "class":
                return symbol
            symbol_id = symbol.parent_id
        return None

    def _bare(
        self, f: FileGraphInput, scope: _Scope, name: str, source_id: uuid.UUID | None
    ) -> _Resolved | None:
        # Enclosing functions (nested definitions), then the module itself.
        symbol_id = source_id
        while symbol_id is not None:
            symbol = self.symbols[symbol_id]
            if symbol.kind != "class":
                found = self.children[symbol.id].get(name)
                if found is not None:
                    return _Resolved(found, "local")
            symbol_id = symbol.parent_id
        found = self.top_level[f.file_id].get(name)
        if found is not None:
            return _Resolved(found, "local")
        if name in scope.names:
            module, imported = scope.names[name]
            if module is None:
                return None  # imported from a library
            return self._exported(module, imported if imported != "default" else name)
        if name in scope.modules:
            return None  # a module, not a callable
        return self._unique(self.global_names, f, name, "name-match")

    def _exported(self, module: FileGraphInput, name: str) -> _Resolved | None:
        found = self.top_level[module.file_id].get(name)
        if found is not None:
            return _Resolved(found, "imported-name")
        # One hop through a re-export (`__init__.py`, `index.ts`).
        module_scope = self.scopes.get(module.file_id)
        if module_scope is None or name not in module_scope.names:
            return None
        origin, original = module_scope.names[name]
        if origin is None:
            return None
        found = self.top_level[origin.file_id].get(original)
        return _Resolved(found, "re-export") if found else None

    def _unique(
        self,
        index: dict[tuple[str, str], list[SymbolNode]],
        f: FileGraphInput,
        name: str,
        how: str,
        *,
        methods: bool = False,
    ) -> _Resolved | None:
        if methods and (name in COMMON_METHODS or len(name) < 4 or name.startswith("__")):
            return None
        matches = index.get((self.file_family[f.file_id], name), [])
        return _Resolved(matches[0], how) if len(matches) == 1 else None

    # --- summary -------------------------------------------------------------------------------

    def _stats(self) -> dict[str, Any]:
        keys = (
            "imports_static",
            "imports_inferred",
            "imports_external",
            "imports_unresolved",
            "calls_static",
            "calls_inferred",
            "calls_unresolved",
            "inheritance_static",
            "inheritance_inferred",
            "inheritance_unresolved",
            "dynamic_imports",
        )
        return {"edges": len(self.edges)} | {k: self.stats.get(k, 0) for k in keys}

    def _warnings(self) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        if self.stats["dynamic_imports"]:
            warnings.append(
                {
                    "code": "graph_dynamic_imports",
                    "count": self.stats["dynamic_imports"],
                    "message": "Some imports are computed at runtime, so the graph cannot follow "
                    "them.",
                    "paths": self.dynamic_paths,
                }
            )
        if self.stats["imports_unresolved"]:
            warnings.append(
                {
                    "code": "graph_unresolved_imports",
                    "count": self.stats["imports_unresolved"],
                    "message": "These local imports did not match a file in the snapshot. Path "
                    "aliases such as @/ are not read from tsconfig yet.",
                    "imports": self.unresolved_imports,
                }
            )
        return warnings


def _shared_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split("/"), b.split("/"), strict=False):
        if x != y:
            break
        n += 1
    return n
