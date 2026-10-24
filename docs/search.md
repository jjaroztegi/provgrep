# Search: what a result is, and what it does not say

## A result is a function, not a file

The index holds the functions and methods of each repository (with the comments right above
them), cut by tree-sitter for C, C++, Go, Java, JavaScript, Python, Rust and TypeScript. Tests,
generated and minified files are left out; vendored copies stay in, as copies.

The same code found in several places is one result. Two copies are the same when they differ
only in indentation, trailing spaces or blank lines. The copy shown is the first one outside a
vendored directory, in the most-starred repository. The others are listed by
`/api/snippets/{id}`, each with its own license.

## Copies can disagree on the license

When copies of one function sit under different licenses, the result says how many licenses
there are (`licenses` > 1) and the filter judges the copy shown. Which copy is the original, and
so which license it was first published under, is not decided yet: provgrep shows the
disagreement instead of resolving it.

## The license filter

- `permissive` (the default): the copy shown is under a permissive or public-domain license, at
  level A, B or C. Level C was measured as reliable as A on Debian's records (see the README).
- `any`: no filter. Code is still shown only under a license that lets it be shared
  (permissive, copyleft, public domain): with no license found, or only proprietary,
  source-available or unknown terms, the result is a link. A shareable license beside other
  terms is enough, since GPL headers also match "commercial" or "proprietary" wording; the whole
  expression is shown either way.
- A list of SPDX ids, such as `MIT,BSD-3-Clause,GPL-2.0-or-later`: the copy shown passes when one
  way to comply with its license needs only those ids. `MIT OR GPL-3.0-only` passes for `MIT`;
  `MIT AND GPL-3.0-only` does not. Ids are compared as written: `GPL-2.0-only` and
  `GPL-2.0-or-later` are different ids. Up to 8 ids.

The levels say where a license comes from: A, the file's own header; B, a license file and a
manifest that agree; C, one of them, or the deeper of two that disagree; X, nothing.

## Notices

Every result carries the copyright statements that go with it: the ones in the file's header, or
else the ones in the license files above it. Email addresses are removed, from notices and from the
comments and docstrings in code; string literals are code and stay. Addresses spelled out in words
("name at host dot org") are not recognized. A statement that is part of a license's own text, like
the Free Software Foundation's in a copy of the GPL, is not a notice for the code and is left out. A
quarter of files have a license and no copyright statement anywhere; their notices are empty, and
the license and the link are what attribution has.

## Queries

Words and identifiers are matched in the function's name, its comments and docstrings, its path
and its code. Identifiers are split into words (`XRenderCreatePicture`: x, render, create,
picture), kept whole, and kept as the family prefixes APIs are named by (`xrender`), so a part,
the whole name or its family all find it. An identifier typed as one in a query weighs three
times a plain word. `require` asks for identifiers that must be in the code, whole, `A|B` for
either: `--require XRenderCreatePicture` in the command line, `require=` in the API. Whole means
case and underscores aside (`parse_rtp_header` is `parseRtpHeader`) but not a part: `wait` does
not match `epoll_wait`.

