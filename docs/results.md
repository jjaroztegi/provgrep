# Results

What provgrep was measured to do, each number with the command, corpus and commit that reproduce
it. Every timing comes from the same server, with 8 cores and 31 GB of memory.

## The index

`corpus/github.json` (sha256 4da3293d763e, made by `provgrep corpus`) pins 10,915 GitHub
repositories, each to a commit:

- the 500 most-starred of each of C, C++, Go, Java, JavaScript, Python, Rust and TypeScript,
  pinned on 2026-10-24;
- every C repository with 1,000 stars or more, and the 300 most-starred with 10 stars or more of
  each of Ada, C3, Common Lisp, D, Erlang, Fortran, GLSL, Haskell, Jai, Julia, Lua, Nim, OCaml,
  Odin, Pascal, Racket, Teal, Vala and Zig, pinned on 2026-10-29.

`provgrep build`, with the code of 7e8a15f, made an index of 32,474,882 distinct functions in
35 GB from it. The numbers below are over that index unless they say otherwise.

## Licenses, against Debian's records

`provgrep evaluate licenses` compares the license provgrep gives each code file with the one
Debian's maintainers record in `debian/copyright`. It covers 300 Python source packages with their
upstream on GitHub: 19,534 code files, Debian snapshot 20261024T000000Z, about 630 MB of downloads.
Debian's license names are mapped to SPDX ids by a table of provgrep's own, so the two sides do not
share a detector's mistakes, and a name the table does not know leaves its file out.

A permissive filter would show 15,555 of the files. Debian does not record 74 of those as
permissive, and each of the 74 was read by hand: Debian's record is stale for 30 (upstream
relicensed since) and coarse for 44 (the files carry their own notices). provgrep is wrong for
none of them. Files where both sides agree were not read. The audit, with the reasons and sources
for each file: [truth/debian-python-audit.json](../truth/debian-python-audit.json).

## Search

`provgrep evaluate search` runs the four suites in `truth/intents/`. Each question comes with the
calls the code of a right answer makes, and a result is a hit when its code makes all of them, so
the suites need no one to grade results by hand. A hit is not proof of the best answer, which is
why the summary lists the hits for reading. "Answerable" counts the questions whose calls some
function in the index makes at all, which tells a miss of the search from a gap in the corpus.

Searched with the code of 5904e2f, queries embedded by the 4-layer distillation:

| suite | licenses | questions | a hit first | a hit in the top 10 | MRR@10 | answerable |
|---|---|---:|---:|---:|---:|---:|
| APIs: common tasks in 8 languages | any | 89 | 41 | 76 | 0.59 | 89 |
| | permissive | 89 | 44 | 77 | 0.61 | 89 |
| XRender: what a live session needed | any | 19 | 8 | 14 | 0.51 | 18 |
| | permissive | 19 | 4 | 13 | 0.36 | 18 |
| Languages: common tasks in 19 more | any | 40 | 16 | 29 | 0.50 | 40 |
| | permissive | 40 | 12 | 31 | 0.43 | 40 |
| raylib: what a C program does with it | any | 15 | 7 | 11 | 0.51 | 15 |
| | permissive | 15 | 7 | 12 | 0.56 | 15 |

Without the order by meaning (the same index without its `dense.json`), a hit came first for 31
and 26 of the API questions (MRR 0.48 and 0.46) and for 10 and 8 of the languages ones, and was in
the top 10 for 14 and 8 of the XRender ones.

The XRender questions are the 19 that came up while [Tsoding](https://github.com/tsoding) wrote an
XRender program in C from scratch, live ([Tsoding Daily](https://www.youtube.com/watch?v=auzZCp45dJc)),
each with the minute it came up. Most of them are answered by C repositories with 1,000 stars or
more: wine, vcxsrv, compton, chadwm and uTox make calls that none of the 500 most-starred C
repositories make. The index answers 18 of the 19 (16 before those repositories were added).
Nothing in it puts a shared memory image on a pixmap and composites it. Some first hits are
vcxsrv's copy of libXrender, which is the call itself rather than code that makes it, and the
grading cannot tell the two apart. The first steps of any X11 program (XSelectInput, XStoreName
with XMapWindow) are in 57 to 169 functions each, and none of them reaches the top 10: X11 window
code that does something else comes first.

The languages suite asks 40 common tasks, each in one of the 19 languages (a gen_server in Erlang,
a counter in Haskell's State monad, a raylib window in Odin). The raylib suite asks what Tsoding's
musializer and panim do with raylib in C, and each question names raylib. Without the name, a hit
was in the top 10 for 2 of the 15: raylib's calls are in 3 to 242 functions each, and SDL, GLFW and
OpenGL code also makes windows, textures and sounds.

The index of the 500 most-starred repositories of the 8 languages alone (21,259,913 functions,
corpus sha256 526db429d94b, built at 378bb39, searched with the code of 21c6088) had 41/76/0.59 and
42/77/0.60 on the APIs, and 3/9/0.28 and 3/8/0.24 on XRender (a hit first, a hit in the top 10 and
MRR@10, with any license and with permissive ones).

## Speed

`provgrep evaluate search truth/intents/apis.json truth/intents/xrender.json`, run a second time,
takes 17 ms a search at the median and 33 ms at the 95th percentile, and 16 and 37 ms without the
order by meaning (the middle of three runs each). It asks each question twice, with any license and
with permissive ones, and the second time the question's embedding is remembered. The order costs
little because the question is embedded by a small model, while the 14 partitions are searched on
the same 8 cores.

## The same code under different licenses

`provgrep copies` counts the functions whose copies disagree on the license. Of the 32.5 million,
4,019,689 appear in more than one repository, and 177,696 of those are under a permissive license
in one place and a copyleft one in another. Most are short helpers that many projects share, but
23,202 are 40 lines or longer and identical apart from whitespace: vendored copies, forks that
relicensed, code moved between projects.

One of them: the same 34 lines of `ConcurrentHashMap.clear` are detected as Apache-2.0 in IntelliJ
IDEA, and as GPL-2.0 with the Classpath exception and CC0-1.0 in OpenJDK and in j2objc, which took
them from OpenJDK. All three files say that Doug Lea released the code to the public domain. In
IntelliJ's copy that comment comes after the `package` line, where provgrep does not read it yet.
A license taken from a repository says nothing about which of these a function is, and provgrep
does not yet say which copy came first.

## What it takes to serve

The index is larger than the server's memory, and the server has other work to do, so serving was
measured for memory and CPU as well as speed. Four changes cut what it takes, each measured against
main at bf7cd5f with that change alone:

- Reading the index a page at a time (#38). tantivy, numpy and the code blocks map the index's
  files, and Linux reads 128 KB around each page a search faults in, while a search reads small
  pieces at random: a term's entry, posting lists, 100 rows of vectors, 10 blocks of code. With
  `MADV_RANDOM` on every map, a cold run of the four suites leaves 2.03 GB of the index in the page
  cache instead of 9.46 GB, and its p95 is 0.23 s instead of 0.30. The second run is as fast (p95
  67 ms against 68), and stays as fast with the process held to 2.5 GB (p95 68 ms). Without the
  change, 8 GB already slows it to a p95 of 244 ms.
- Leaving words like `the`, `to` and `for` out of queries (#39). `the` is in 26% of functions'
  docs and 28% of their code, and every candidate was moved along its postings in four fields of
  each partition. Without them, the two suites above take 27.8 ms at the median and 51.9 at the
  95th percentile against 35.8 and 73.1. Rows of the suites move by one or two either way, with
  the same totals (134 hits first, 259 in the top 10).
- Embedding queries with a 4-layer distillation of the embedding model (#40), published as
  [jina-v2-code-query-4l](https://huggingface.co/jjaroztegi/jina-v2-code-query-4l). A query costs
  a third of the CPU, and with #39 the suites total 139 hits first and 263 in the top 10. Its card
  says how it was trained; `provgrep distill` makes it again ([docs/build.md](build.md#embeddings)).
- Letting the query encoder's threads sleep between queries (#36). onnxruntime's threads spin
  while they wait for work by default, on the cores the partitions' searches need at that moment.

Together, the four suites' second run went from 32.6 ms at the median and 69.0 at the 95th
percentile (bf7cd5f) to 15.5 and 33.2 (the code of 5904e2f).

Measuring memory this way needs the index's pages out of the page cache first (`posix_fadvise`
with `POSIX_FADV_DONTNEED` on each of its files, with no other process mapping them), then two runs
in one cgroup, whose `memory.stat` says what the runs left:

```sh
systemd-run --user --wait -p MemoryHigh=2.5G -p MemorySwapMax=0 \
    sh -c 'provgrep evaluate search; provgrep evaluate search'
```

The load test that drove this work found a bug: searches running at the same time shared a
partition's SQLite statement, and some failed with a 500. Each connection now runs one statement at
a time (#35).
