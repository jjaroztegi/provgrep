# Building and serving an index

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

A function goes to a partition by the hash of its code, so all its copies are in the same one.
Each partition dedups, picks the copy to show and counts copies on its own, and the partitions are
built at once, each with a fraction of the memory the whole would need. Each one is also a random
sample of the corpus, so the ranking statistics it scores with are the whole index's. An index
grows by adding partitions (`PARTITION_SIZE` copies of functions each, in `build.py`), and a search
asks them all at once.

## Caches, apart from the index

Two things outlive any one index, in `PROVGREP_CACHE` (by default the user cache directory, such
as `~/.cache/provgrep`). Both can sit on a slow disk:

- `staging/<key>/`: each repository fetched and resolved, one file per repository and commit.
  The key hashes the code and tool versions that stage, so a change to any of them stages
  everything again rather than mixing outputs; files under another key are removed.
- `embeddings/<model>/<run>/`: every embedding the model has made, by the hash of the code.

## On several machines

Staging is most of a build's time, and each repository is independent. `provgrep stage --part
I/N` stages every Nth repository of the corpus from the Ith. Copy the staged files of every part
into one cache's `staging/<key>/` (the same key: the same code and tool versions) and run
`provgrep build` there, which stages only what is missing.

## Embeddings

`provgrep embed` gives an index the embeddings that order results by meaning. The embeddings in an
index belong to one build of it: after a rebuild, the index does not open until `provgrep embed`
has run again. That run takes the vectors of any code an earlier run embedded from the cache, and
the model embeds only code no run has seen, so a rebuild of mostly the same code takes minutes.

An index keeps each vector smaller than the model makes it: projected on the 256 directions the
vectors vary most along, as int8. The projection is a PCA fitted once on the cache, so vectors
projected at any time compare. 128 to 384 directions rank the suites as well as all 768 do; 256
keep 96% of the variance, in a third of the size.

On a GPU, code is embedded in half precision with the CUDA build of onnxruntime in place of the
CPU one:

```sh
uv sync
uv pip uninstall onnxruntime
uv pip install "onnxruntime-gpu[cuda,cudnn]==1.31.0"
uv run --no-sync provgrep embed --gpu  # if stopped, it goes on where it was
```

Embedding the 11.2 million functions that the corpus' later tiers added to the 21.3 million of the
first (corpus sha256 4da3293d763e) took 8 hours on one 16 GB NVIDIA GPU, an RTX A4000: 9,639,242 in
25,105 s (the `new` and `seconds` of the run's `dense.json`) after 1,578,089 in a first run, 390
functions a second. At that rate the 32.5 million from nothing would take about 23 hours.

Under WSL, NVIDIA's driver kills a run now and then (6 times in the 15 hours of the first index's
run, 2 in the 8 of this one), with an illegal instruction or a segmentation fault while copying a
batch's vectors off the GPU. What a run saved before a crash is kept and taken up by the next run,
so run it again until it exits with 0.

The query model, [jina-v2-code-query-4l](https://huggingface.co/jjaroztegi/jina-v2-code-query-4l),
is made from the staged corpus and the embeddings cache by `provgrep distill`. It took 37 minutes
on an RTX A4000 for the corpus of sha256 4da3293d763e, and the result ranked the suites within
noise of the published model. Training on a GPU is not byte for byte the same twice.

```sh
uv pip install torch --index-url https://download.pytorch.org/whl/cu128
uv pip install "transformers==4.57.6" onnx
uv run --no-sync provgrep distill --out distilled  # if stopped, it keeps what the model embedded
```

## Removals

`corpus/removed.json` lists what people asked to have left out: whole repositories, files or
directories of one, and functions by id, each with the request it answers. Every build leaves
them out (a repository is not even fetched) and counts what it left out in the manifest.

## Serving

`provgrep serve` listens on 127.0.0.1. To listen on another address (`--host`), give the URL
people reach the site at (`--public https://...`): MCP refuses requests addressed to any other host
or sent from another site's pages. `--public` is also the URL the site gives crawlers and link
previews (sitemap, canonical links, the preview image); without it, the site uses the address a
request came to.

Each address may search twice a second, 30 at once, and the server takes 64 connections at a time;
wider limits belong in the proxy in front of it. The server sees the address the proxy forwards
only if the proxy connects from 127.0.0.1 or ::1. From anywhere else (a container), name the proxy
in `FORWARDED_ALLOW_IPS` (an address or a network), or every visitor shares the proxy's budget.
Nothing about a search is logged, but a proxy that logs requests logs what was searched.

A search reads small parts of every partition, a page at a time on Linux, so the first searches on
a new index wait on the disk for what they need, and only that stays in memory. There is no need
to read the index before serving it. The 35 GB index serves at full speed in 2.5 GB of memory, the
process's own included, and from a cold page cache the four suites took p95 0.23 s
([results](results.md#what-it-takes-to-serve)).

The image `ghcr.io/jjaroztegi/provgrep`, built from `Dockerfile` for every commit on main
(`sha-<commit>`, `main`) and every version tag, holds the server, its query model and the grammars
that colour code. It needs no network and writes nothing, so it runs read-only as an unprivileged
user (65532), with the index mounted:

```sh
docker run --read-only -p 127.0.0.1:8000:8000 -v "$PWD/index/github:/index" \
    ghcr.io/jjaroztegi/provgrep:main serve --index /index --host 0.0.0.0 --public https://...
```

The mount cannot be read-only: tantivy opens `.tantivy-meta.lock` for writing to open an index,
even to read it. `provgrep build` leaves that lock writable by anyone and every other file readable
by anyone, so the image's user reads the index and takes the lock but changes nothing else. An
index built before that needs `chmod -R a+rX` on it and `chmod a+w` on its
`partitions/*/lexical/.tantivy-meta.lock`.

A change to the index's format needs the code and the index switched together, so pin the image by
its digest and change it with the index.
