# provgrep

Search open-source code by what it does, and get back real functions, each with the license it is
under, the evidence for that license, its copyright notices and an attribution to paste with it.
A search website, with a command line and an MCP server on the same engine.

The idea is to search for code instead of generating it: find what people already wrote and
published for the same task, and keep its authors' credit and license with it. You describe what
you want to do; provgrep finds the functions that have your words, orders the first 100 by how
close their code is to what you asked, and shows each one with where it came from. Code with no
license that lets it be shared is linked to, not shown. Licenses are as detected, with their
evidence, and they are not legal advice.

## Where it started

[Tsoding](https://github.com/tsoding) wrote an XRender program in C from scratch, live
([Tsoding Daily](https://www.youtube.com/watch?v=auzZCp45dJc)), from documentation that answers
little. Most of what had to be found out was already in code that makes the same calls, in wine,
vcxsrv, compton and other programs. The 19 questions of that session, each with the minute it came
up, became one of provgrep's test suites, and the index grew for them: every C repository with
1,000 stars or more, which answers 18 of the 19, and 19 more languages Tsoding writes in.

## What it found

The index holds 32,474,882 distinct functions from 10,915 GitHub repositories, each pinned to a
commit: the 500 most-starred of each of C, C++, Go, Java, JavaScript, Python, Rust and TypeScript,
every C repository with 1,000 stars or more, and the 300 most-starred of each of 19 more languages.

Licenses were checked against what Debian's maintainers record for 300 Python packages. Of the
15,555 files a permissive filter would show, Debian records 74 under a license that is not
permissive; read by hand, Debian's record is stale or coarse for all 74, and provgrep is wrong for
none.

Search is measured on four suites of questions, where a result is an answer when its code makes
the calls a right answer makes. Counting only code under a permissive license:

| suite | questions | an answer first | an answer in the top 10 |
|---|---:|---:|---:|
| APIs: common tasks in 8 languages | 89 | 44 | 77 |
| Languages: common tasks in 19 more | 40 | 12 | 31 |
| raylib: what a C program does with it | 15 | 7 | 12 |
| XRender: what the live session needed | 19 | 4 | 13 |

The same code can be under different licenses. 4,019,689 functions appear in more than one
repository, and 177,696 of those are under a permissive license in one and a copyleft one in
another, 23,202 of them 40 lines or longer: vendored copies, forks that relicensed, code moved
between projects.

A search takes 17 ms at the median and 33 ms at the 95th percentile on an 8-core server, and
serving the 35 GB index at full speed takes 2.5 GB of memory. Queries are embedded by
[jina-v2-code-query-4l](https://huggingface.co/jjaroztegi/jina-v2-code-query-4l), a 4-layer
distillation of jina-embeddings-v2-base-code made for provgrep, which embeds a query at a third of
the full model's CPU.

Every number with the command, corpus and commit that reproduce it:
[docs/results.md](docs/results.md).

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

`provgrep mcp` serves the same search to an agent over stdio.

## Documentation

- [Search](docs/search.md): what a result is, the license filters, notices, queries, and what a
  result does not say
- [Results](docs/results.md): what was measured, and how serving fits in 2.5 GB
- [Building and serving an index](docs/build.md): caches, several machines, embeddings, removals

## Removal

To have a repository, a file or a function left out of the index, or to report personal data in a
result, [open a removal request](https://github.com/jjaroztegi/provgrep/issues/new?template=removal.yml).

## Development

```sh
uv sync
uv run ruff format && uv run ruff check --fix
uv run mypy
uv run pytest
```

## License

Apache-2.0, except the OSADL License Compatibility Matrix in `src/provgrep/osadl/`, under
CC-BY-4.0, and the Iosevka fonts in `src/provgrep/static/fonts/`, under OFL-1.1
([REUSE.toml](REUSE.toml)).
