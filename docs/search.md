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
- A project's license (`license=compatible&project=GPL-3.0-or-later`, `--project` in the command
  line, `project` in MCP): the copy shown passes when the [OSADL License Compatibility
  Matrix](https://www.osadl.org/Access-to-raw-data.oss-compliance-raw-data-access.0.html) says
  code under its license may go into a project under that one. Each result carries OSADL's
  explanation and the matrix's timestamp. Only a plain Yes passes. "Check dependency" does not:
  Apache-2.0 code goes into a GPL-2.0-or-later project only if the whole is GPL-3.0-or-later.
  Neither does a license the matrix lacks (119 are in it; CC0-1.0 and ScanCode's `LicenseRef-`
  ones are not), so 6 to 7% of shareable code is never shown this way. An exception the matrix
  lacks (`GPL-2.0-only WITH Linux-syscall-note`) takes its license's answer, unless that answer
  is No, since the exception may be what lifts it. A closed-source project has no row: use
  `permissive`. The matrix (`src/provgrep/osadl/`) is OSADL's, a project of the Open Source
  Automation Development Lab (OSADL) eG, under CC-BY-4.0; the verdicts are its claims, not ours.

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
times a plain word. A result has at least one of the query's less common terms: a word found in
more than 1% of functions (`get`, `data`) adds to the rank of a result that has a rarer one, but
does not bring in results by itself, unless every word of the query is that common. `require`
asks for identifiers that must be in the code, whole, `A|B` for either: `--require
XRenderCreatePicture` in the command line, `require=` in the API. Whole means case and
underscores aside (`parse_rtp_header` is `parseRtpHeader`) but not a part: `wait` does not match
`epoll_wait`.

## Ordering by meaning

When the index has embeddings of its code (`provgrep embed`), the first 100 results of a query
are put in order of how close each function's code is to the query in meaning, as
[jina-embeddings-v2-base-code](https://huggingface.co/jinaai/jina-embeddings-v2-base-code)
embeds them; later results keep the lexical order. Lexical search still decides what is found:
a function with none of the query's terms is not a result, however close its meaning. A
result's score is then the cosine of its code with the query, not BM25. Only the first 512
tokens of a long function are embedded.

Embedding the 21 million functions of the full index takes about two days on one 16 GB NVIDIA
GPU (an RTX A4000), with the CUDA build of onnxruntime in place of the CPU one:

```sh
uv sync
uv pip uninstall onnxruntime
uv pip install "onnxruntime-gpu[cuda,cudnn]==1.31.0"
uv run --no-sync provgrep embed --gpu  # stopped, it goes on where it was
```

The embeddings belong to one build of the index: after a rebuild, the index does not open until
`provgrep embed` has run again. That run takes the vectors of the code it already had, by the
code's hash, and embeds only what is new.

## Measuring it

`provgrep evaluate search` runs the suites in `truth/intents/`. An intent is a question and the
identifiers a right answer's code contains; a result is a hit when its code has all of them.
That grades without anyone reading results, but a hit is not proof of the best answer: the
summary lists them to be read. Coverage counts the snippets that contain the identifiers at all,
so a miss can be told from a corpus without the answer.
