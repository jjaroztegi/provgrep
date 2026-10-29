"""Code cut into runs of text by what its syntax makes them (keyword, string, comment, number,
type), to colour as the Gruber Darker theme does. Each grammar ships queries that name its
nodes; they are written for later patterns to override earlier ones ((identifier) @variable
first, the special cases after), and that is how they are applied. Done on the server, with the
grammars the index was cut with, so a page needs no script for it."""

import re
from functools import cache, lru_cache
from importlib import resources
from itertools import pairwise
from pathlib import PurePosixPath

from tree_sitter import Language, Parser, Query, QueryCursor

from provgrep.chunks import LANGUAGES

# The class site.css colours each capture with, by its name or the start of it ("type.builtin"
# is a "type"); anything else is plain text.
_CLASSES = {
    "keyword": "k",
    "operator": "k",
    "punctuation.special": "k",  # ${ } around a template's expressions
    "variable.builtin": "k",  # this, super
    "tag": "k",  # JSX's elements
    "string": "s",
    "comment": "c",
    "number": "n",
    "constant": "n",
    "escape": "n",
    "string.escape": "n",
    "function.macro": "n",
    "type": "t",
    "constructor": "t",
    "attribute": "t",
}
# Which highlight queries each grammar takes, as (package, file): C++'s and TypeScript's add to
# C's and JavaScript's.
_JSX = ("tree_sitter_javascript", "highlights-jsx.scm")
_QUERIES = {
    "python": [("tree_sitter_python", "highlights.scm")],
    "c": [("tree_sitter_c", "highlights.scm")],
    "cpp": [("tree_sitter_c", "highlights.scm"), ("tree_sitter_cpp", "highlights.scm")],
    "javascript": [("tree_sitter_javascript", "highlights.scm"), _JSX],
    "typescript": [
        ("tree_sitter_javascript", "highlights.scm"),
        ("tree_sitter_typescript", "highlights.scm"),
    ],
    "tsx": [
        ("tree_sitter_javascript", "highlights.scm"),
        _JSX,
        ("tree_sitter_typescript", "highlights.scm"),
    ],
    "go": [("tree_sitter_go", "highlights.scm")],
    "rust": [("tree_sitter_rust", "highlights.scm")],
    "java": [("tree_sitter_java", "highlights.scm")],
}
_BY_SUFFIX = {suffix: key for key, language in LANGUAGES.items() for suffix in language.suffixes}

_CAPTURE = re.compile(rb"@([\w.]+)")
# A method cut out of its class does not parse alone in these: it is parsed back inside one.
_IN_CLASS = {"java", "javascript", "typescript", "tsx"}
_CLASS = (b"class _ {\n", b"\n}")

Run = tuple[str, str]  # a colour class ("" for plain text) and its text


@lru_cache(maxsize=2048)  # the same results come back as a query is typed
def runs(path: str, code: str) -> tuple[Run, ...]:
    """The code as runs of text, each with the class of its colour, in order."""
    key = _BY_SUFFIX.get(PurePosixPath(path).suffix.lower())
    if key is None:
        return (("", code),)
    language, query = _grammar(key)
    data = code.encode()
    parser = Parser(language)  # one a call: pages are made on several threads
    tree, shift = parser.parse(data), 0
    if tree.root_node.has_error and key in _IN_CLASS:
        tree, shift = parser.parse(_CLASS[0] + data + _CLASS[1]), len(_CLASS[0])
    found = []
    for pattern, captures in QueryCursor(query).matches(tree.root_node):
        for name, nodes in captures.items():
            for node in nodes:
                start = max(node.start_byte - shift, 0)
                end = min(node.end_byte - shift, len(data))
                if start < end:  # not the class put around it
                    found.append((start, -end, pattern, end, _class(name)))
    # Outer nodes first and inner ones over them; on one node, later patterns over earlier ones.
    paint = [""] * len(data)
    for start, _, _, end, cls in sorted(found):
        paint[start:end] = [cls] * (end - start)
    cuts = sorted({0, len(data), *(f[0] for f in found), *(f[3] for f in found)})
    out: list[Run] = []
    for start, end in pairwise(cuts):
        text = data[start:end].decode(errors="replace")
        if out and out[-1][0] == paint[start]:
            out[-1] = (paint[start], out[-1][1] + text)
        else:
            out.append((paint[start], text))
    return tuple(out)


@cache
def _grammar(key: str) -> tuple[Language, Query]:
    language = Language(LANGUAGES[key].grammar())
    source = "\n".join(
        (resources.files(package) / "queries" / name).read_text(encoding="utf-8")
        for package, name in _QUERIES[key]
    )
    query = Query(language, source)
    # Most matches are of patterns that only name what stays plain (variables, calls, brackets):
    # without them a query runs in about half the time, and plain text loses nothing.
    data = source.encode()
    for i in range(query.pattern_count):  # type: ignore[call-overload]  # a property, not a method
        pattern = data[query.start_byte_for_pattern(i) : query.end_byte_for_pattern(i)]
        if not any(_class(name.decode()) for name in _CAPTURE.findall(pattern)):
            query.disable_pattern(i)
    return language, query


@cache
def _class(capture: str) -> str:
    parts = capture.split(".")
    for n in range(len(parts), 0, -1):
        if (cls := _CLASSES.get(".".join(parts[:n]))) is not None:
            return cls
    return ""
