"""The units the index holds: the functions and methods of a source file, and which files count.

A unit is a function or method as whole lines, with the comments, attributes and decorators right
above it. A nested function belongs to the unit that contains it, unless that one is longer than
MAX_LINES (a module wrapper, a generated table), in which case the functions inside it are units.
Units are keyed by a hash of their normalized code, so the same function found in several places,
at any indentation, is one snippet with several locations.

What a unit is, per language, is in LANGUAGES: tree-sitter queries over the official grammars.
"""

import bisect
import hashlib
import re
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import PurePosixPath

import tree_sitter_c
import tree_sitter_cpp
import tree_sitter_go
import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_rust
import tree_sitter_typescript
from tree_sitter import Language as Grammar
from tree_sitter import Node, Parser, Query, QueryCursor

from provgrep import without_emails

# Lines with more than brackets on them: fewer are getters, stubs and one-liners, in any brace
# style (`int x() {` / `return x;` / `}` is two).
MIN_LINES = 3
MAX_LINES = 300


@dataclass(frozen=True)
class Language:
    name: str  # what the index and its filters call it
    grammar: Callable[[], object]
    suffixes: tuple[str, ...]
    # @unit: the lines of a unit; @name: its name, else found through C declarators;
    # @scope: a name it is qualified with, like a Go method's receiver type.
    units: str
    # @container with @name: what qualifies the names of units inside it (classes, impls).
    containers: str
    # @doc: comments and docstrings, the words people write about code.
    docs: str
    tests: re.Pattern[str]  # file names of tests


_JS_UNITS = """
(function_declaration name: (_) @name) @unit
(generator_function_declaration name: (_) @name) @unit
(method_definition name: (_) @name) @unit
(export_statement declaration: (function_declaration name: (_) @name)) @unit
(lexical_declaration (variable_declarator
  name: (_) @name value: [(arrow_function) (function_expression)])) @unit
(variable_declaration (variable_declarator
  name: (_) @name value: [(arrow_function) (function_expression)])) @unit
(export_statement declaration: (lexical_declaration (variable_declarator
  name: (_) @name value: [(arrow_function) (function_expression)]))) @unit
(expression_statement (assignment_expression
  left: (_) @name right: [(arrow_function) (function_expression)])) @unit
(pair key: (_) @name value: [(arrow_function) (function_expression)]) @unit
"""
_JS_CONTAINERS = """
(class_declaration name: (_) @name) @container
(class name: (_) @name) @container
"""
_TS_CONTAINERS = _JS_CONTAINERS + "(abstract_class_declaration name: (_) @name) @container"
_JS_TESTS = re.compile(r".*\.(test|spec)\.[cm]?[jt]sx?")
_C_TESTS = re.compile(r"(test_.*|.*_(unit)?test)\.(c|cc|cpp|cxx)")

LANGUAGES = {
    "python": Language(
        "python",
        tree_sitter_python.language,
        (".py",),
        """
        (function_definition name: (_) @name) @unit
        (decorated_definition definition: (function_definition name: (_) @name)) @unit
        """,
        "(class_definition name: (_) @name) @container",
        """
        (comment) @doc
        (function_definition body: (block . (expression_statement (string (string_content) @doc))))
        """,
        re.compile(r"test_.*\.py|.*_test\.py|conftest\.py"),
    ),
    "c": Language(
        "c",
        tree_sitter_c.language,
        (".c", ".h"),
        "(function_definition) @unit",
        "",
        "(comment) @doc",
        _C_TESTS,
    ),
    "cpp": Language(
        "cpp",
        tree_sitter_cpp.language,
        (".cc", ".cpp", ".cxx", ".c++", ".hh", ".hpp", ".hxx", ".h++"),
        """
        (function_definition) @unit
        (template_declaration (function_definition)) @unit
        """,
        """
        (class_specifier name: (_) @name body: (_)) @container
        (struct_specifier name: (_) @name body: (_)) @container
        (namespace_definition name: (_) @name) @container
        """,
        "(comment) @doc",
        _C_TESTS,
    ),
    "javascript": Language(
        "javascript",
        tree_sitter_javascript.language,
        (".js", ".mjs", ".cjs", ".jsx"),
        _JS_UNITS,
        _JS_CONTAINERS,
        "(comment) @doc",
        _JS_TESTS,
    ),
    "typescript": Language(
        "typescript",
        tree_sitter_typescript.language_typescript,
        (".ts", ".mts", ".cts"),
        _JS_UNITS,
        _TS_CONTAINERS,
        "(comment) @doc",
        _JS_TESTS,
    ),
    "tsx": Language(
        "typescript",
        tree_sitter_typescript.language_tsx,
        (".tsx",),
        _JS_UNITS,
        _TS_CONTAINERS,
        "(comment) @doc",
        _JS_TESTS,
    ),
    "go": Language(
        "go",
        tree_sitter_go.language,
        (".go",),
        """
        (function_declaration name: (_) @name body: (_)) @unit
        (method_declaration
          receiver: (parameter_list (parameter_declaration type: (_) @scope))
          name: (_) @name body: (_)) @unit
        """,
        "",
        "(comment) @doc",
        re.compile(r".*_test\.go"),
    ),
    "rust": Language(
        "rust",
        tree_sitter_rust.language,
        (".rs",),
        "(function_item name: (_) @name) @unit",
        """
        (impl_item type: (_) @name) @container
        (trait_item name: (_) @name) @container
        (mod_item name: (_) @name) @container
        """,
        "[(line_comment) (block_comment)] @doc",
        re.compile(r"tests?\.rs"),
    ),
    "java": Language(
        "java",
        tree_sitter_java.language,
        (".java",),
        """
        (method_declaration name: (_) @name body: (_)) @unit
        (constructor_declaration name: (_) @name) @unit
        """,
        """
        (class_declaration name: (_) @name) @container
        (interface_declaration name: (_) @name) @container
        (enum_declaration name: (_) @name) @container
        (record_declaration name: (_) @name) @container
        """,
        "[(line_comment) (block_comment)] @doc",
        re.compile(r".*Tests?\.java"),
    ),
}
_BY_SUFFIX = {suffix: key for key, lang in LANGUAGES.items() for suffix in lang.suffixes}
SUFFIXES = frozenset(_BY_SUFFIX)
CPP_SUFFIXES = frozenset(LANGUAGES["cpp"].suffixes)

_TEST_DIRS = frozenset(
    {"test", "tests", "testing", "unittests", "__tests__", "spec", "testdata"}
    | {"fixtures", "__fixtures__", "__mocks__"}
)
_VENDORED_DIRS = frozenset(
    {
        "vendor",
        "vendored",
        "_vendor",
        "third_party",
        "thirdparty",
        "3rdparty",
        "external",
        "extern",
        "node_modules",
        "bower_components",
    }
)
_GENERATED_NAME = re.compile(r".*(_pb2(_grpc)?\.py|\.pb\.(go|cc|h)|\.min\.js|_generated\.\w+)")
# A comment near the top that says so. Docstrings are left alone: they say "generated by" of
# what a function returns.
_GENERATED = re.compile(
    r"^\s*(#|//|/\*|\*|--).*(\bauto-?generated\b|\bgenerated by\b|\bdo not edit\b|@generated)",
    re.IGNORECASE,
)
_MAX_FILE = 1 << 20
PARSE_SECONDS = 10.0
_DATA_LINE = re.compile(r"\s*((0[xX][0-9a-fA-F]+|-?\d+(\.\d+)?[fFuUlL]*)\s*,\s*)+\S{0,20}\s*")
# Rust test modules and C++ test namespaces: units inside them are tests.
_TEST_SCOPES = frozenset({"test", "tests"})
_SCOPE_NAME = re.compile(r"[*&\s]*([\w:.$]+)")


@dataclass(frozen=True)
class Unit:
    symbol: str  # qualified within its file: `Class.method`
    start: int  # first line, 1-based
    end: int  # last line
    code: str  # the lines, dedented
    doc: str  # its comments and docstrings
    hash: str  # sha256 of the normalized code


def language_of(rel: PurePosixPath, cpp: bool) -> str | None:
    """The key in LANGUAGES for a file; `.h` is C++ in a tree that has C++ sources."""
    suffix = rel.suffix.lower()
    if suffix == ".h" and cpp:
        return "cpp"
    return _BY_SUFFIX.get(suffix)


def kind(rel: PurePosixPath, text: str, language: str) -> str:
    """ "test", "generated", "vendored" or "code". Tests and generated files are not indexed;
    vendored files are, as copies of code that has a home elsewhere."""
    dirs = {part.lower() for part in rel.parts[:-1]}
    if dirs & _TEST_DIRS or LANGUAGES[language].tests.fullmatch(rel.name):
        return "test"
    if _GENERATED_NAME.fullmatch(rel.name) or dirs & {"migrations", "dist"}:
        return "generated"
    head = text.split("\n", 10)[:10]
    if len(text) > _MAX_FILE or any(_GENERATED.match(line) for line in head):
        return "generated"
    if len(text) > 4096 and len(text) > 300 * (text.count("\n") + 1):
        return "generated"  # minified: lines of hundreds of characters on average
    if len(text) > 16384 and _data(text):
        return "generated"  # a font, an image, a certificate store as numbers
    if dirs & _VENDORED_DIRS:
        return "vendored"
    return "code"


def units(text: str, language: str) -> list[Unit]:
    """The functions and methods of a source file, of MIN_LINES to MAX_LINES lines."""
    source = text.encode()
    # Rows as tree-sitter counts them: split on "\n" only, unlike str.splitlines.
    lines = text.split("\n")
    deadline = time.monotonic() + PARSE_SECONDS
    try:
        # A byte array in a file of its own, parsed as C++, took 372 s: what does not parse in
        # PARSE_SECONDS is data, not code. Only parsing from a callback can be stopped.
        tree = _parser(language).parse(
            lambda at, _: source[at : at + (1 << 16)],
            progress_callback=lambda *_: time.monotonic() > deadline,
        )  # nodes do not keep their tree alive
    except ValueError:
        return []
    root = tree.root_node

    found: dict[int, tuple[Node, Node | None, Node | None]] = {}
    for _, match in QueryCursor(_query(language, "units")).matches(root):
        node = match["unit"][0]
        if node.id not in found:
            found[node.id] = (node, _first(match, "name"), _first(match, "scope"))
    containers = sorted(
        (m["container"][0].start_byte, m["container"][0].end_byte, _scope_name(m["name"][0]))
        for _, m in QueryCursor(_query(language, "containers")).matches(root)
        if "name" in m
    )
    docs = sorted(
        (n.start_byte, n.end_byte, without_emails(_text(n)))
        for n in QueryCursor(_query(language, "docs")).captures(root).get("doc", [])
    )
    doc_starts = [d[0] for d in docs]
    lines = _without_emails_in_docs(source, docs) if docs else lines

    out: list[Unit] = []
    last_end = -1
    for node, name, scope in sorted(
        found.values(), key=lambda f: (f[0].start_byte, -f[0].end_byte)
    ):
        if node.end_byte <= last_end:
            continue  # inside a unit already taken
        length = node.end_point.row - node.start_point.row + 1
        if length > MAX_LINES:
            continue  # the units inside it are taken instead
        last_end = node.end_byte
        outer = [
            n for start, end, n in containers if start <= node.start_byte and node.end_byte <= end
        ]
        own = lines[node.start_point.row : node.end_point.row + 1]
        if _substantive(own) < MIN_LINES or _TEST_SCOPES & {n.lower() for n in outer}:
            continue
        first = _leading(node, root, lines)
        lo = bisect.bisect_left(doc_starts, first.start_byte)
        hi = bisect.bisect_left(doc_starts, node.end_byte)
        doc = "\n".join(t for _, end, t in docs[lo:hi] if end <= node.end_byte)
        code = textwrap.dedent("\n".join(lines[first.start_point.row : node.end_point.row + 1]))
        qualified = [*outer, *([_scope_name(scope)] if scope else []), _name(name, node)]
        out.append(
            Unit(
                ".".join(qualified),
                first.start_point.row + 1,
                node.end_point.row + 1,
                code,
                doc,
                hashlib.sha256(normalized(code).encode()).hexdigest(),
            )
        )
    return out


def _without_emails_in_docs(source: bytes, docs: list[tuple[int, int, str]]) -> list[str]:
    """The lines of source with email addresses left out of its comments and docstrings, the
    prose that names authors. String literals are code and stay as they are."""
    pieces = []
    last = 0
    for start, end, text in docs:
        if start >= last:
            pieces += [source[last:start], text.encode()]
            last = end
    pieces.append(source[last:])
    return b"".join(pieces).decode(errors="replace").split("\n")


def normalized(code: str) -> str:
    """Code with trailing spaces and blank lines left out; copies differ in those."""
    return "\n".join(s for line in textwrap.dedent(code).split("\n") if (s := line.rstrip()))


def _data(text: str) -> bool:
    """Whether most of the first lines are lists of numbers: an embedded binary, not code."""
    lines = [line for line in text.split("\n", 400)[:400] if line.strip()]
    return sum(bool(_DATA_LINE.fullmatch(line)) for line in lines) > len(lines) / 2


def _substantive(lines: list[str]) -> int:
    return sum(bool(line.strip(" \t\r{}()[];,")) for line in lines)


def _leading(node: Node, root: Node, lines: list[str]) -> Node:
    """The first of the comments and attributes right above a unit, each on its own line, or the
    unit itself. A run of comments that reaches the top of the file is the file's header."""
    first = node
    prev = node.prev_named_sibling
    while (
        prev is not None
        and ("comment" in prev.type or prev.type == "attribute_item")
        and prev.end_point.row >= first.start_point.row - 1
        and not lines[prev.start_point.row][: prev.start_point.column].strip()
    ):
        first = prev
        prev = prev.prev_named_sibling
    if prev is None and first is not node and node.parent is not None and node.parent == root:
        return node
    return first


def _name(name: Node | None, node: Node) -> str:
    """A unit's name: the captured one, else the innermost C declarator (`Surface::draw` in
    `static int *Surface::draw(int x)`)."""
    if name is not None:
        return " ".join(_text(name).split())
    current = node
    for _ in range(16):
        nxt = current.child_by_field_name("declarator") or next(
            (
                c
                for c in current.named_children
                if c.type.endswith("declarator") or c.type == "function_definition"
            ),
            None,
        )
        if nxt is None:
            return " ".join(_text(current).split())
        current = nxt
    return "?"


def _scope_name(node: Node) -> str:
    """`Server` for `*Server[T]` or `Foo<T>`: what a reader calls the type."""
    m = _SCOPE_NAME.match(_text(node))
    return m.group(1) if m else _text(node)


def _first(match: dict[str, list[Node]], capture: str) -> Node | None:
    nodes = match.get(capture)
    return nodes[0] if nodes else None


def _text(node: Node) -> str:
    return (node.text or b"").decode(errors="replace")


@cache
def _grammar(language: str) -> Grammar:
    return Grammar(LANGUAGES[language].grammar())


@cache
def _parser(language: str) -> Parser:
    return Parser(_grammar(language))


@cache
def _query(language: str, kind: str) -> Query:
    source: str = getattr(LANGUAGES[language], kind)
    # An empty query is not valid; one that matches nothing stands in for it.
    return Query(_grammar(language), source.strip() or '((ERROR) @none (#eq? @none ""))')
