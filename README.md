# provgrep

Search open-source code by what it does and get back the real snippet: the license it is under
with the evidence for it, where else the same code is, and an attribution to paste with it. A
search website, with a command line and an MCP server on the same engine.

Status: an index of the 500 most-starred GitHub repositories of each of C, C++, Go, Java,
JavaScript, Python, Rust and TypeScript, pinned on 2026-10-24: 21,259,913 distinct functions,
found by their words and ordered by meaning, from a local website, the command line, a JSON API
and MCP. Which copy of a function came first is not decided yet.

## Licenses, measured

Every code file gets a license from its own header, the nearest license file above it, or the
nearest package manifest, with the evidence and a level for how much of it agrees (A: the file's
header; B: license file and manifest agree; C: one source, or they disagree; X: nothing found).

Against the licenses Debian maintainers record in `debian/copyright` for 300 Python source
packages from GitHub upstreams (19,534 code files, Debian snapshot 20261024T000000Z): of 15,555
files a permissive filter would show, 74 are under a license Debian does not record as
permissive. Each of the 74 was read by hand. Debian's record is stale for 30 (upstream
relicensed since) and coarse for 44 (the files carry their own notices); provgrep is wrong for
none. Files where both sides agree were not read. The audit, with its reasons and sources:
[truth/debian-python-audit.json](truth/debian-python-audit.json).

## Search, measured

Two suites of intents in `truth/intents/`, each a question and the calls the code of a right
answer contains; a result is a hit when its code has them all. Over the full index (built and
searched at 378bb39 from corpus/github.json, sha256 526db429d94b, with its embeddings; queries
embedded with the model's 8-bit weights):

| suite | licenses | intents | a hit first | a hit in the top 10 | MRR@10 | answerable from the index |
|---|---|---:|---:|---:|---:|---:|
| APIs: common tasks in 8 languages | any | 89 | 41 | 76 | 0.59 | 89 |
| | permissive | 89 | 42 | 77 | 0.60 | 89 |
| XRender: what a live session needed | any | 8 | 1 | 4 | 0.24 | 6 |
| | permissive | 8 | 2 | 2 | 0.25 | 5 |

Search finds the functions that have the question's words (BM25 over identifiers split into words
and kept whole, in partitions searched at once), then orders the first 100 by how close their code
is to the question in meaning ([docs/search.md](docs/search.md#ordering-by-meaning)). Without that
order, a hit came first for 33 and 30 of the API intents (MRR 0.49 and 0.48) and in the top 10
for no XRender question with any license. On the 8-core server, once the index has been read from
disk, a search takes 28 ms at the median and 60 ms at the 95th percentile (23 and 53 ms without
the order); the benchmark asks each question twice, with any license and with permissive ones,
and the second time its embedding is remembered.

The XRender questions are the ones a programmer had while writing an XRender program in C from
scratch, live ([Tsoding Daily](https://www.youtube.com/watch?v=auzZCp45dJc)). The index answers 6
of the 8, with a handful of functions each among 21 million; search puts one in the top 10 for 4.
Some answers exist only under copyleft: the only three functions that fill a rectangle with
`XRenderFillRectangle` and an `XRenderColor` are in TigerVNC (GPL-2.0-or-later) and the OpenJDK
(GPL-2.0 with the Classpath exception). The OpenJDK's comes first for that question, and a project
under MIT gets nothing to copy for it, and is told so.

## The same code under different licenses

Of the 21.3 million functions, 1,817,661 appear in more than one repository, and 71,154 of those are
under a permissive license in one place and a copyleft one in another (`provgrep copies`, over the
same index). Most are short helpers many projects share, but 5,308 are 40 lines or longer, identical
but for whitespace: vendored copies, forks that relicensed, code moved between projects. One
example: the same 34 lines of `ConcurrentHashMap.clear` are under Apache-2.0 in IntelliJ IDEA and
under GPL-2.0 with the Classpath exception and CC0-1.0 in OpenJDK and in j2objc, which took them
from it. All three files say that Doug Lea released the code to the public domain; in IntelliJ's
the comment comes after the `package` line, where provgrep does not read it yet. A license taken from a repository
says nothing about which of these a function is, and provgrep does not yet say which copy came
first.

## Start

Needs [uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
uv run provgrep license PATH                # the license of every code file under PATH
uv run provgrep evaluate licenses --jobs 8  # the Debian measurement: ~630 MB of downloads
uv run provgrep build --jobs 8              # the pinned corpus: ~3 h on 16 threads, 26 GB of index
uv run provgrep serve                       # the website at http://127.0.0.1:8000, MCP at /mcp
uv run provgrep search "parse an RTP header" --license any
uv run provgrep evaluate search             # the suites in truth/intents/
uv run provgrep copies                      # functions whose copies disagree on the license
```

`provgrep mcp` serves the same search to an agent over stdio. How results, filters and notices
work, and what they do not say: [docs/search.md](docs/search.md). How an index is built, split
across machines and served: [docs/build.md](docs/build.md).

Licenses are as detected, with their evidence. They are not legal advice.
