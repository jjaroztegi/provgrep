# Building an index

`provgrep build` turns a pinned corpus (`corpus/github.json`, made by `provgrep corpus`) into an
index directory that `provgrep serve`, `search` and `mcp` read and never write.

## What an index holds

- `sources.sqlite`: the repositories and their files, each file with its license, the evidence
  for it and its copyright notices.
- `partitions/<n>/`: `records.sqlite` (the distinct functions and where each one is found),
  `code.zst` and `code.npy` (their code, 16 functions to a zstd frame, compressed with
  `code.zdict`) and `lexical/` (the search index over them, in one segment). With embeddings,
  also `vectors.npy`.
- `manifest.json`: the corpus, code and tool versions it was built from, its counts, and what
  was left out and why.

A function goes to a partition by the hash of its code, so all its copies are in the same one:
each partition dedups, picks the copy to show and counts copies alone, and the partitions are
built at once, each with a fraction of the memory the whole would need. Each one is also a random
sample of the corpus, so the ranking statistics it scores with are the whole index's. An index
grows by adding partitions (`PARTITION_SIZE` copies of functions each, in `build.py`); a search
asks them all at once.

## Caches, apart from the index

Two things outlive any one index, in `PROVGREP_CACHE` (by default the user cache directory, e.g.
`~/.cache/provgrep`). Both can sit on a slow disk:

- `staging/<key>/`: each repository fetched and resolved, one file per repository and commit.
  The key hashes the code and tool versions that stage, so a change to any of them stages
  everything again rather than mixing outputs; files under another key are removed.
- `embeddings/<model>/<run>/`: every embedding the model has made, by the hash of the code. A
  rebuild takes its vectors from here and embeds only code no run has seen.

## On several machines

Staging is most of a build's time, and each repository is independent. `provgrep stage --part
I/N` stages every Nth repository of the corpus from the Ith; copy the staged files of every part
into one cache's `staging/<key>/` (the same key: the same code and tool versions) and run
`provgrep build` there, which stages only what is missing.

## Removals

`corpus/removed.json` lists what people asked to have left out: whole repositories, files or
directories of one, and functions by id, each with the request it answers. Every build leaves
them out (a repository is not even fetched) and counts what it left out in the manifest.

## Serving

`provgrep serve` listens on 127.0.0.1. To listen on another address (`--host`), give the URL
people reach the site at (`--public https://...`): MCP refuses requests addressed to any other host
or sent from another site's pages. Each address may search twice a second, 30 at once, and the
server takes 64 connections at a time; wider limits belong in the proxy in front of it. Nothing
about a search is logged.

A search reads small parts of every partition, a page at a time on Linux, so the first searches on
a new index wait on the disk for what they need, and only that stays in memory: there is no need to
read the index before serving it. On the index of 10,915 repositories (corpus sha256
4da3293d763e, 35 GB), `provgrep evaluate search` over the four suites took p95 0.23 s from a cold
page cache and left 2.0 GB of the index in it; run again, p95 67 ms, as fast with the process's
`memory.high` (cgroup v2) at 2.5 GB. Read ahead in pages around each one, the first run left
9.5 GB, and with 8 GB the second took p95 0.24 s.
