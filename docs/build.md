# Building an index

`provgrep build` turns a pinned corpus (`corpus/github.json`, made by `provgrep corpus`) into an
index directory that `provgrep serve`, `search` and `mcp` read and never write.

## What an index holds

- `sources.sqlite`: the repositories and their files, each file with its license, the evidence
  for it and its copyright notices.
- `partitions/<n>/`: `records.sqlite` (the distinct functions, their code compressed with
  `code.zdict`, and where each one is found) and `lexical/` (the search index over them). With
  embeddings, also `vectors.npy`.
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
