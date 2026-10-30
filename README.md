# provgrep

Search open-source code by what it does and get back the real snippet: the license it is under
with the evidence for it, where else the same code is, and an attribution to paste with it. A
search website, with a command line and an MCP server on the same engine.

Status: an index of 10,915 GitHub repositories, each pinned to a commit: the 500 most-starred of
each of C, C++, Go, Java, JavaScript, Python, Rust and TypeScript (2026-10-24), then every C
repository with 1,000 stars or more and the 300 most-starred with 10 or more of each of Ada, C3,
Common Lisp, D, Erlang, Fortran, GLSL, Haskell, Jai, Julia, Lua, Nim, OCaml, Odin, Pascal, Racket,
Teal, Vala and Zig, the languages [Tsoding](https://github.com/tsoding) writes in beside C
(2026-10-29). 32,474,882 distinct functions, found by their words and ordered by meaning, from a
local website, the command line, a JSON API and MCP. Which copy of a function came first is not
decided yet.

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

Four suites of intents in `truth/intents/`, each a question and the calls the code of a right answer
contains; a result is a hit when its code has them all. Over the full index (built from
corpus/github.json, sha256 4da3293d763e, at 53082cf, which is the code of 7e8a15f, with its
embeddings, and searched at f697c4b; queries embedded by the model's 4-layer distillation):

| suite | licenses | intents | a hit first | a hit in the top 10 | MRR@10 | answerable from the index |
|---|---|---:|---:|---:|---:|---:|
| APIs: common tasks in 8 languages | any | 89 | 41 | 76 | 0.59 | 89 |
| | permissive | 89 | 44 | 77 | 0.61 | 89 |
| XRender: what a live session needed | any | 19 | 8 | 14 | 0.51 | 18 |
| | permissive | 19 | 4 | 13 | 0.36 | 18 |
| Languages: common tasks in 19 more | any | 40 | 16 | 29 | 0.50 | 40 |
| | permissive | 40 | 12 | 31 | 0.43 | 40 |
| raylib: what a C program does with it | any | 15 | 7 | 11 | 0.51 | 15 |
| | permissive | 15 | 7 | 12 | 0.56 | 15 |

Search finds the functions that have the question's words (BM25 over identifiers split into words
and kept whole, in partitions searched at once), then orders the first 100 by how close their code
is to the question in meaning ([docs/search.md](docs/search.md#ordering-by-meaning)). Without that
order (the same index without its `dense.json`), a hit came first for 31 and 26 of the API intents
(MRR 0.48 and 0.46), 10 and 8 of the languages, and in the top 10 for 14 and 8 XRender questions. On
the 8-core server (31 GB), `provgrep evaluate search truth/intents/apis.json
truth/intents/xrender.json` run a second time gives a search 17 ms at the median and 33 ms at the
95th percentile (16 and 37 ms without the order; the middle of three runs); it asks each question
twice, with any license and with permissive ones, and the second time its embedding is remembered.
The order costs little: the question is embedded by a 4-layer distillation of the model, beside the
searches of 14 partitions on the server's 8 cores. The index of the 500 most-starred repositories of
8 languages alone (21,259,913 functions, built at 378bb39 from corpus sha256 526db429d94b, searched
at c1aaef1) had 41/76/0.59 and 42/77/0.60 on the APIs and 3/9/0.28 and 3/8/0.24 on XRender, in 28
and 60 ms.

The XRender questions are the 19 a programmer had while writing an XRender program in C from
scratch, live ([Tsoding Daily](https://www.youtube.com/watch?v=auzZCp45dJc)), each with the time it
came up. Every C repository with 1,000 stars or more is what answers most of them: wine, vcxsrv,
compton, chadwm and uTox call what the 500 most-starred C repositories did not, and the index
answers 18 of the 19 (16 before), one in the top 10 for 14. A shared memory image put on a pixmap
and composited is in no function. Some first hits are vcxsrv's copy of libXrender, the call itself
rather than code that makes it, which the grading cannot tell apart. The first steps of any X11
program (XSelectInput, XStoreName with XMapWindow) are in 57 to 169 functions each, none of them in
the top 10, where X11 window code that does something else comes first.

The languages suite asks 40 common tasks of the 19 languages, each in its own (a gen_server in
Erlang, a counter in Haskell's State monad, a raylib window in Odin). The raylib suite asks what
Tsoding's musializer and panim do with raylib in C, each question naming it; without the name, a hit
was in the top 10 for 2 of the 15: raylib's calls are in 3 to 242 functions each, and a window, a
texture or a sound are what SDL, GLFW and OpenGL code makes too.

## The same code under different licenses

Of the 32.5 million functions, 4,019,689 appear in more than one repository, and 177,696 of those
are under a permissive license in one place and a copyleft one in another (`provgrep copies`, over
the same index). Most are short helpers many projects share, but 23,202 are 40 lines or longer,
identical but for whitespace: vendored copies, forks that relicensed, code moved between projects.
One example: the same 34 lines of `ConcurrentHashMap.clear` are detected as Apache-2.0 in IntelliJ
IDEA and as GPL-2.0 with the Classpath exception and CC0-1.0 in OpenJDK and in j2objc, which took
them from it. All three files say that Doug Lea released the code to the public domain; in
IntelliJ's the comment comes after the `package` line, where provgrep does not read it yet. A
license taken from a repository says nothing about which of these a function is, and provgrep does
not yet say which copy came first.

## Start

Needs [uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
uv run provgrep license PATH                # the license of every code file under PATH
uv run provgrep evaluate licenses --jobs 8  # the Debian measurement: ~630 MB of downloads
uv run provgrep build --jobs 8              # the pinned corpus: ~5 h on 16 threads, 35 GB of index
uv run provgrep serve                       # the website at http://127.0.0.1:8000, MCP at /mcp
uv run provgrep search "parse an RTP header" --license any
uv run provgrep evaluate search             # the suites in truth/intents/
uv run provgrep copies                      # functions whose copies disagree on the license
```

`provgrep mcp` serves the same search to an agent over stdio. How results, filters and notices
work, and what they do not say: [docs/search.md](docs/search.md). How an index is built, split
across machines and served: [docs/build.md](docs/build.md).

Licenses are as detected, with their evidence. They are not legal advice.
