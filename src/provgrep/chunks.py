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
from dataclasses import dataclass
from functools import cache
from pathlib import PurePosixPath

import tree_sitter_language_pack
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
    grammar: str  # its name in tree-sitter-language-pack
    suffixes: tuple[str, ...]
    # @unit: the lines of a unit; @name: its name, else found through C declarators;
    # @scope: a name it is qualified with, like a Go method's receiver type.
    units: str
    # @container with @name: what qualifies the names of units inside it (classes, impls).
    containers: str
    # @doc: comments and docstrings, the words people write about code.
    docs: str
    tests: re.Pattern[str]  # file names of tests
    # Nodes right above a unit that are part of it, besides comments: Rust's attributes,
    # Haskell's type signatures, Julia's docstrings.
    leading: tuple[str, ...] = ()
    # Units of one name in a row are one: the equations of a Haskell function.
    clauses: bool = False


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
        "python",
        (".py",),
        """
        (function_definition name: (_) @name) @unit
        (decorated_definition definition: (function_definition name: (_) @name)) @unit
        """,
        "(class_definition name: (_) @name) @container",
        """
        (comment) @doc
        (function_definition body: (block . (string (string_content) @doc)))
        """,
        re.compile(r"test_.*\.py|.*_test\.py|conftest\.py"),
    ),
    "c": Language(
        "c",
        "c",
        (".c", ".h"),
        "(function_definition) @unit",
        "",
        "(comment) @doc",
        _C_TESTS,
    ),
    "cpp": Language(
        "cpp",
        "cpp",
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
        "javascript",
        (".js", ".mjs", ".cjs", ".jsx"),
        _JS_UNITS,
        _JS_CONTAINERS,
        "(comment) @doc",
        _JS_TESTS,
    ),
    "typescript": Language(
        "typescript",
        "typescript",
        (".ts", ".mts", ".cts"),
        _JS_UNITS,
        _TS_CONTAINERS,
        "(comment) @doc",
        _JS_TESTS,
    ),
    "tsx": Language(
        "typescript",
        "tsx",
        (".tsx",),
        _JS_UNITS,
        _TS_CONTAINERS,
        "(comment) @doc",
        _JS_TESTS,
    ),
    "go": Language(
        "go",
        "go",
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
        "rust",
        (".rs",),
        "(function_item name: (_) @name) @unit",
        """
        (impl_item type: (_) @name) @container
        (trait_item name: (_) @name) @container
        (mod_item name: (_) @name) @container
        """,
        "[(line_comment) (block_comment)] @doc",
        re.compile(r"tests?\.rs"),
        leading=("attribute_item",),
    ),
    "java": Language(
        "java",
        "java",
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
    # The languages below are the ones Tsoding writes in (github.com/tsoding), and a few more of
    # their kind. Each is cut into what it calls a function, procedure or subprogram.
    "zig": Language(
        "zig",
        "zig",
        (".zig",),
        "(Decl (FnProto function: (_) @name) (Block)) @unit",
        """
        (VarDecl variable_type_function: (_) @name
          (ErrorUnionExpr (SuffixExpr (ContainerDecl)))) @container
        """,
        "[(line_comment) (doc_comment) (container_doc_comment)] @doc",
        re.compile(r"test_.*\.zig|.*_test\.zig"),
    ),
    "odin": Language(
        "odin",
        "odin",
        (".odin",),
        "(procedure_declaration . (identifier) @name (procedure (block))) @unit",
        "",
        "[(comment) (block_comment)] @doc",
        re.compile(r".*_test\.odin"),
    ),
    "c3": Language(
        "c3",
        "c3",
        (".c3", ".c3i"),
        """
        (func_definition (func_header method_type: (_) @scope name: (_) @name) body: (_)) @unit
        (func_definition (func_header !method_type name: (_) @name) body: (_)) @unit
        (macro_declaration (macro_header method_type: (_) @scope name: (_) @name) body: (_)) @unit
        (macro_declaration (macro_header !method_type name: (_) @name) body: (_)) @unit
        """,
        "",
        "[(line_comment) (block_comment) (doc_comment)] @doc",
        re.compile(r"test_.*\.c3|.*_test\.c3"),
    ),
    "jai": Language(
        "jai",
        "jai",
        (".jai",),
        "(procedure_declaration name: (_) @name (block)) @unit",
        "",
        "[(comment) (block_comment)] @doc",
        re.compile(r"test_.*\.jai|.*_test\.jai"),
    ),
    "nim": Language(
        "nim",
        "nim",
        (".nim",),
        # A routine is the only child of its statement, the sibling of the comments above it.
        "(stmt . (routine . (keyw) . (symbol) @name (block)) .) @unit",
        "",
        "[(comment) (docComment) (multilineComment) (multilineDocComment)] @doc",
        re.compile(r"test_.*\.nim|.*_test\.nim"),
    ),
    "d": Language(
        "d",
        "d",
        (".d",),
        """
        (function_declaration (identifier) @name (function_body)) @unit
        (constructor (this) @name (function_body)) @unit
        """,
        """
        (class_declaration . (class) . (identifier) @name) @container
        (struct_declaration . (struct) . (identifier) @name) @container
        (interface_declaration . (interface) . (identifier) @name) @container
        (union_declaration . (union) . (identifier) @name) @container
        """,
        "(comment) @doc",
        re.compile(r"test_.*\.d|.*_test\.d"),
    ),
    "ada": Language(
        "ada",
        "ada",
        (".adb", ".ada"),
        """
        (subprogram_body (function_specification name: (_) @name)) @unit
        (subprogram_body (procedure_specification name: (_) @name)) @unit
        """,
        """
        (package_body name: (_) @name) @container
        (protected_body . (identifier) @name) @container
        (task_body . (identifier) @name) @container
        """,
        "(comment) @doc",
        re.compile(r"test_.*\.adb|.*[-_]tests?\.adb"),
    ),
    "pascal": Language(
        "pascal",
        "pascal",
        (".pas", ".pp", ".dpr", ".lpr"),
        "(defProc header: (declProc name: (_) @name)) @unit",
        "",
        "(comment) @doc",
        re.compile(r"[Tt]est.*\.(pas|pp)|.*Tests?\.(pas|pp)"),
    ),
    "fortran": Language(
        "fortran",
        "fortran",
        (".f90", ".f95", ".f03", ".f08", ".f", ".for"),
        """
        (function (function_statement name: (_) @name)) @unit
        (subroutine (subroutine_statement name: (_) @name)) @unit
        (module_procedure (module_procedure_statement name: (_) @name)) @unit
        (program (program_statement (name) @name)) @unit
        """,
        """
        (module (module_statement (name) @name)) @container
        (submodule (submodule_statement (name) @name)) @container
        """,
        "(comment) @doc",
        re.compile(r"test_.*\.f\w*|.*_test\.f\w*"),
    ),
    "erlang": Language(
        "erlang",
        "erlang",
        (".erl",),
        "(fun_decl . (function_clause name: (_) @name)) @unit",
        "",
        "(comment) @doc",
        re.compile(r".*_SUITE\.erl|.*_tests?\.erl"),
    ),
    "haskell": Language(
        "haskell",
        "haskell",
        (".hs",),
        """
        (function name: (_) @name) @unit
        (bind name: (_) @name) @unit
        """,
        """
        (class name: (_) @name) @container
        (instance name: (_) @name) @container
        """,
        "[(comment) (haddock)] @doc",
        re.compile(r".*Spec\.hs|.*Tests?\.hs"),
        leading=("signature", "haddock"),
        clauses=True,
    ),
    "ocaml": Language(
        "ocaml",
        "ocaml",
        (".ml",),
        """
        (value_definition (let_binding pattern: (_) @name (parameter))) @unit
        (value_definition (let_binding pattern: (_) @name
          body: [(fun_expression) (function_expression)])) @unit
        (value_definition (let_binding pattern: (unit) @name)) @unit
        (method_definition (method_name) @name) @unit
        """,
        """
        (module_binding (module_name) @name) @container
        (class_binding (class_name) @name) @container
        """,
        "(comment) @doc",
        re.compile(r"test_.*\.ml|.*_test\.ml"),
    ),
    "lua": Language(
        "lua",
        "lua",
        (".lua",),
        """
        (function_declaration name: (_) @name) @unit
        (assignment_statement (variable_list name: (_) @name)
          (expression_list value: (function_definition))) @unit
        (field name: (_) @name value: (function_definition)) @unit
        """,
        "",
        "(comment) @doc",
        re.compile(r".*_spec\.lua|test_.*\.lua|.*_test\.lua"),
    ),
    "teal": Language(
        "teal",
        "teal",
        (".tl",),
        "(function_statement name: (_) @name) @unit",
        "",
        "(comment) @doc",
        re.compile(r".*_spec\.tl|test_.*\.tl"),
    ),
    "julia": Language(
        "julia",
        "julia",
        (".jl",),
        """
        (function_definition (signature (call_expression . (_) @name))) @unit
        (function_definition (signature (typed_expression . (call_expression . (_) @name)))) @unit
        (function_definition (signature (where_expression . (call_expression . (_) @name)))) @unit
        (macro_definition (signature (call_expression . (_) @name))) @unit
        """,
        "(module_definition name: (_) @name) @container",
        """
        [(line_comment) (block_comment)] @doc
        ((string_literal) @doc . [(function_definition) (macro_definition)])
        """,
        re.compile(r"runtests\.jl|test_.*\.jl"),
        leading=("string_literal",),  # a docstring
    ),
    "vala": Language(
        "vala",
        "vala",
        (".vala",),
        """
        (function_definition name: (_) @name) @unit
        (class_constructor_definition name: (_) @name) @unit
        """,
        """
        (class_declaration (identifier) @name) @container
        (namespace (identifier) @name) @container
        """,
        "(comment) @doc",
        re.compile(r"test_.*\.vala|.*[-_]test\.vala"),
    ),
    "racket": Language(
        "racket",
        "racket",
        (".rkt",),
        """
        (list . (symbol) @define . (list . (symbol) @name)
          (#any-of? @define "define" "define/contract" "define/public" "define/private"
            "define/override" "define-syntax-rule")) @unit
        """,
        "",
        "[(comment) (block_comment)] @doc",
        re.compile(r"test-.*\.rkt|.*-tests?\.rkt"),
    ),
    "commonlisp": Language(
        "commonlisp",
        "commonlisp",
        (".lisp", ".lsp"),
        "(list_lit . (defun (defun_header function_name: (_) @name))) @unit",
        "",
        """
        [(comment) (block_comment)] @doc
        (defun (defun_header) . (str_lit) @doc)
        """,
        re.compile(r"test-.*\.lisp|.*-tests?\.lisp"),
    ),
    "glsl": Language(
        "glsl",
        "glsl",
        (".glsl", ".vert", ".frag", ".geom", ".comp", ".tesc", ".tese", ".vs", ".fs"),
        "(function_definition) @unit",
        "",
        "(comment) @doc",
        re.compile(r"(?!)"),  # shaders have no test files
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
# Bytes of source handed to the parser at a time. py-tree-sitter 0.25 leaks each piece a parse
# reads (py-tree-sitter#498), and some grammars read the same source again and again: D's read
# a 405 KB file 27,833 times, and in 64 KB pieces kept 1.6 GB of it. In 1 KB pieces it kept 29
# MB and parsed 12 times as fast; C and C++ parse as fast either way.
READ = 1 << 10
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
            lambda at, _: source[at : at + READ],
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
    named: dict[int, tuple[int, int, str]] = {}  # the first name found for each container
    # A query that stood in for a language's having none matched ERROR nodes by their text, and
    # on a data file read as a GLSL shader took hours.
    if LANGUAGES[language].containers:
        for _, m in QueryCursor(_query(language, "containers")).matches(root):
            if "name" in m and (box := m["container"][0]).id not in named:
                named[box.id] = (box.start_byte, box.end_byte, _scope_name(m["name"][0], source))
    containers = sorted(named.values())
    docs = sorted(
        (n.start_byte, n.end_byte, without_emails(_text(n, source)))
        for n in QueryCursor(_query(language, "docs")).captures(root).get("doc", [])
    )
    doc_starts = [d[0] for d in docs]
    lines = _without_emails_in_docs(source, docs) if docs else lines

    leading = LANGUAGES[language].leading
    ordered = sorted(found.values(), key=lambda f: (f[0].start_byte, -f[0].end_byte))
    runs = (
        _clauses(ordered, source, leading)
        if LANGUAGES[language].clauses
        else [(node, node, name, scope) for node, name, scope in ordered]
    )
    out: list[Unit] = []
    last_end = -1
    for node, last, name, scope in runs:
        if last.end_byte <= last_end:
            continue  # inside a unit already taken
        length = last.end_point.row - node.start_point.row + 1
        if length > MAX_LINES:
            continue  # the units inside it are taken instead
        last_end = last.end_byte
        outer = [
            n for start, end, n in containers if start <= node.start_byte and last.end_byte <= end
        ]
        own = lines[node.start_point.row : last.end_point.row + 1]
        if _substantive(own) < MIN_LINES or _TEST_SCOPES & {n.lower() for n in outer}:
            continue
        first = _leading(node, root, lines, leading)
        lo = bisect.bisect_left(doc_starts, first.start_byte)
        hi = bisect.bisect_left(doc_starts, last.end_byte)
        doc = "\n".join(t for _, end, t in docs[lo:hi] if end <= last.end_byte)
        code = textwrap.dedent("\n".join(lines[first.start_point.row : last.end_point.row + 1]))
        scoped = [_scope_name(scope, source)] if scope else []
        qualified = [*outer, *scoped, _name(name, node, source)]
        out.append(
            Unit(
                ".".join(qualified),
                first.start_point.row + 1,
                last.end_point.row + 1,
                code,
                doc,
                hashlib.sha256(normalized(code).encode()).hexdigest(),
            )
        )
    return out


def _clauses(
    found: list[tuple[Node, Node | None, Node | None]], source: bytes, leading: tuple[str, ...]
) -> list[tuple[Node, Node, Node | None, Node | None]]:
    """Units as runs of the same name, one after another with only comments and leading nodes
    between them: (first, last, name, scope)."""
    runs: list[tuple[Node, Node, Node | None, Node | None]] = []
    for node, name, scope in found:
        if runs and name is not None and (top := runs[-1])[2] is not None:
            prev = node.prev_named_sibling
            while prev is not None and _leads(prev, leading):
                prev = prev.prev_named_sibling
            if prev == top[1] and _text(name, source) == _text(top[2], source):
                runs[-1] = (top[0], node, top[2], top[3])
                continue
        runs.append((node, node, name, scope))
    return runs


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


def _leading(node: Node, root: Node, lines: list[str], leading: tuple[str, ...]) -> Node:
    """The first of the comments and leading nodes right above a unit, each on its own line, or
    the unit itself. A run of comments that reaches the top of the file is the file's header."""
    first = node
    prev = node.prev_named_sibling
    while (
        prev is not None
        and _leads(prev, leading)
        and prev.end_point.row >= first.start_point.row - 1
        and not lines[prev.start_point.row][: prev.start_point.column].strip()
    ):
        first = prev
        prev = prev.prev_named_sibling
    if prev is None and first is not node and node.parent is not None and node.parent == root:
        return node
    return first


def _leads(node: Node, leading: tuple[str, ...]) -> bool:
    return "comment" in node.type or node.type in leading


def _name(name: Node | None, node: Node, source: bytes) -> str:
    """A unit's name: the captured one, else the innermost C declarator (`Surface::draw` in
    `static int *Surface::draw(int x)`)."""
    if name is not None:
        return " ".join(_text(name, source).split())
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
            return " ".join(_text(current, source).split())
        current = nxt
    return "?"


def _scope_name(node: Node, source: bytes) -> str:
    """`Server` for `*Server[T]` or `Foo<T>`: what a reader calls the type."""
    m = _SCOPE_NAME.match(_text(node, source))
    return m.group(1) if m else _text(node, source)


def _first(match: dict[str, list[Node]], capture: str) -> Node | None:
    nodes = match.get(capture)
    return nodes[0] if nodes else None


def _text(node: Node, source: bytes) -> str:
    # Not node.text: on a tree parsed from a callback it reads the source again through it,
    # and py-tree-sitter 0.25 leaks what it reads (460 MB in 13,800 calls on a 60 KB file).
    return source[node.start_byte : node.end_byte].decode(errors="replace")


@cache
def _grammar(language: str) -> Grammar:
    return tree_sitter_language_pack.get_language(LANGUAGES[language].grammar)


@cache
def _parser(language: str) -> Parser:
    return Parser(_grammar(language))


@cache
def _query(language: str, kind: str) -> Query:
    source: str = getattr(LANGUAGES[language], kind)
    return Query(_grammar(language), source)
