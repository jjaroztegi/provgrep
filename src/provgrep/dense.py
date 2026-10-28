"""Order a search's lexical candidates by meaning: how close the embedding of each one's code is
to the query's.

One pinned model (MODEL) makes the embeddings, run by onnxruntime: on a GPU for the corpus
(`provgrep embed`, about 15 hours for 21 million functions), on the CPU for a query.

What the GPU makes is kept in the cache, apart from any index: embeddings/<model>/<n>/ holds
keys.npy, the first 8 bytes of each function's code hash, and vectors.npy, its embedding as int8
(a unit vector times 127), one directory per run. An index takes from there the vectors of the
code it has, and the GPU embeds only code no run has seen: a rebuild of mostly the same code
costs minutes, not a day. An interrupted run goes on where it stopped.

An index holds them smaller: projected on the KEPT directions they vary most along (PCA, fitted
once on a sample of the store, so vectors projected at any time compare), as int8. In the index,
pca.npz is the projection, partitions/<n>/vectors.npy has row i for snippet i of the partition,
and dense.json says which build of the index the rows are for: rows of another build would rank
the wrong code.
"""

import json
import time
from collections.abc import Callable
from compression import zstd
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

from provgrep import search, source_commit
from provgrep.fetch import cache_dir, download

MAX_TOKENS = 512  # longer code is embedded by its first MAX_TOKENS tokens
# A batch is texts of like length, up to this many tokens with padding: unsorted, an A4000 runs
# a quarter as fast, most of its work spent on padding.
BATCH_TOKENS = 64 * MAX_TOKENS
MAX_BATCH = 256
PART = 100_000  # snippets embedded between two saves
# A query is a few tokens: on 16 threads it took 47 ms at the median and 133 ms at the 95th
# percentile, on 4 threads 22 and 32 ms.
QUERY_THREADS = 4
_BLOCK = 100_000  # rows copied at a time
# Measured on the intent suites, re-ranking the lexical top 100: 128 to 384 dimensions rank as
# well as 768 (40/75/.58 apis any at 768, 41/74/.58 at 256); 256 keep 96% of the variance.
KEPT = 256
FIT = (400, 500)  # blocks of rows sampled from the store to fit the projection on, and their size


@dataclass(frozen=True)
class Model:
    repo: str  # on Hugging Face
    revision: str
    onnx: tuple[str, str]  # path in the repository, sha256
    onnx_fp16: tuple[str, str]  # the same in half precision, for a GPU
    tokenizer: tuple[str, str]
    pad: str  # the token that pads a batch
    dimensions: int
    query_prefix: str
    pooling: str  # "mean" of the tokens' states, or "cls": the first one's


# Chosen over CodeRankEmbed, gte-modernbert-base and granite-embedding-english-r2 by re-ranking
# the lexical top 100 of the intent suites: as good as the best (whose lead was within noise,
# and which has no ONNX file of its own to pin), the fastest on a CPU for a query.
MODEL = Model(
    repo="jinaai/jina-embeddings-v2-base-code",
    revision="516f4baf13dec4ddddda8631e019b5737c8bc250",
    onnx=("onnx/model.onnx", "63363fc178428b74620c6f3780cbc7191883fa5c7f84c0945c45eb5c4256733b"),
    onnx_fp16=(
        "onnx/model_fp16.onnx",
        "1aafc4fcd63d2e6899e88402ff731e7c646c2e435048294a3cbc908a40d45d7c",
    ),
    tokenizer=(
        "tokenizer.json",
        "b01c78a902aa4facb2f47f95449f48e2f7bbfea5d2472ee2f6ce92323c6f86e5",
    ),
    pad="<pad>",
    dimensions=768,
    query_prefix="",
    pooling="mean",
)


class Encoder:
    """MODEL on onnxruntime: texts in, unit vectors out."""

    def __init__(self, gpu: bool = False) -> None:
        import onnxruntime
        from tokenizers import Tokenizer

        onnx, tokenizer = _files(gpu)
        if gpu:
            onnxruntime.preload_dlls()  # the CUDA libraries are wheels, not on the loader's path
        providers = ["CUDAExecutionProvider" if gpu else "CPUExecutionProvider"]
        options = onnxruntime.SessionOptions()
        if not gpu:
            options.intra_op_num_threads = QUERY_THREADS
        self._session = onnxruntime.InferenceSession(str(onnx), options, providers=providers)
        if self._session.get_providers()[0] != providers[0]:  # it falls back without a word
            raise RuntimeError(f"onnxruntime cannot use {providers[0]} here")
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(str(tokenizer))
        self._tokenizer.enable_truncation(MAX_TOKENS)  # the model takes 8192, slowly
        self._pad = self._tokenizer.token_to_id(MODEL.pad)

    def __call__(self, texts: list[str]) -> Any:
        """The unit vectors of texts, in their order."""
        encoded = [e.ids for e in self._tokenizer.encode_batch(texts)]
        batches: list[list[int]] = [[]]
        for i in sorted(range(len(texts)), key=lambda i: len(encoded[i])):
            if (
                len(batches[-1]) == MAX_BATCH
                or (len(batches[-1]) + 1) * len(encoded[i]) > BATCH_TOKENS
            ):
                batches.append([])
            batches[-1].append(i)
        out = np.zeros((len(texts), MODEL.dimensions), dtype=np.float32)
        for batch in batches:
            if batch:
                out[batch] = self._run([encoded[i] for i in batch])
        return out

    def query(self, text: str) -> Any:
        return self([MODEL.query_prefix + text])[0]

    def _run(self, encoded: list[list[int]]) -> Any:
        ids = np.full((len(encoded), max(map(len, encoded))), self._pad, dtype=np.int64)
        mask = np.zeros_like(ids)
        for row, tokens in enumerate(encoded):
            ids[row, : len(tokens)] = tokens
            mask[row, : len(tokens)] = 1
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        states = self._session.run(None, feed)[0].astype(np.float32)
        if MODEL.pooling == "cls":
            pooled = states[:, 0]
        else:
            weights = mask[:, :, None].astype(np.float32)
            pooled = (states * weights).sum(axis=1) / weights.sum(axis=1)
        return pooled / np.linalg.norm(pooled, axis=1, keepdims=True)


class Vectors:
    """An index's embeddings, if they are for its build."""

    def __init__(self, index: Path, built: str, partitions: int) -> None:
        self.about = json.loads((index / "dense.json").read_text(encoding="utf-8"))
        if self.about["index_built"] != built:
            raise ValueError(
                f"the embeddings are for the index built {self.about['index_built']}, not"
                f" {built}: run provgrep embed"
            )
        self._projection = Projection.load(index / "pca.npz")
        self._vectors = [
            np.load(index / "partitions" / str(n) / "vectors.npy", mmap_mode="r")
            for n in range(partitions)
        ]

    def similarity(self, query: Any, snippets: list[tuple[int, int]]) -> list[float]:
        """The cosine of each snippet's code, (partition, id), with the query, both projected (0
        for one not embedded)."""
        if not snippets:
            return []
        rows = np.stack([self._vectors[n][i] for n, i in snippets]) / self._projection.scale
        meaning = query @ self._projection.components
        norms = np.linalg.norm(rows, axis=1) * np.linalg.norm(meaning)
        return [float(c) for c in rows @ meaning / np.maximum(norms, 1e-9)]


@dataclass(frozen=True)
class Projection:
    """The KEPT directions embeddings vary most along, and the scale that makes each int8."""

    components: Any  # dimensions x KEPT
    scale: Any  # KEPT

    @classmethod
    def fit(cls, rows: Any) -> Projection:
        """Fitted on unit vectors as int8, not centred: a cosine of two projections is then
        close to theirs."""
        _, _, directions = np.linalg.svd(rows.astype(np.float32) / 127, full_matrices=False)
        components = np.zeros((rows.shape[1], KEPT), dtype=np.float32)
        components[:, : len(directions)] = directions[:KEPT].T  # fewer rows: fewer directions
        top = np.abs(rows.astype(np.float32) / 127 @ components).max(axis=0)
        return cls(components, 127 / np.where(top > 1e-6, top, 1))  # none: a direction unused

    @classmethod
    def load(cls, path: Path) -> Projection:
        with np.load(path) as kept:
            return cls(kept["components"], kept["scale"])

    def save(self, path: Path) -> None:
        with path.open("wb") as f:
            np.savez(f, components=self.components, scale=self.scale)

    def __call__(self, rows: Any) -> Any:
        """int8 rows of the store, projected to int8 rows of an index."""
        projected = rows.astype(np.float32) / 127 @ self.components * self.scale
        return np.clip(np.rint(projected), -127, 127).astype(np.int8)


class Store:
    """The embeddings every run has made, by key: the first 8 bytes of the code's hash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self._runs = []
        for run in sorted(
            (p for p in path.iterdir() if p.name.isdigit()), key=lambda p: int(p.name)
        ):
            keys = np.load(run / "keys.npy")
            order = np.argsort(keys)
            self._runs.append((keys[order], order, np.load(run / "vectors.npy", mmap_mode="r")))

    def has(self, keys: Any) -> Any:
        """Which keys a run embedded (never 0: row 0, or an id no snippet has)."""
        found = np.zeros(len(keys), dtype=np.bool_)
        for sorted_keys, _, _ in self._runs:
            at = np.minimum(np.searchsorted(sorted_keys, keys), len(sorted_keys) - 1)
            found |= (sorted_keys[at] == keys) & (keys != 0)
        return found

    def take(self, keys: Any) -> Any:
        """The vectors of keys, zero for a key no run embedded."""
        out = np.zeros((len(keys), MODEL.dimensions), dtype=np.int8)
        for sorted_keys, order, stored in self._runs:
            at = np.minimum(np.searchsorted(sorted_keys, keys), len(sorted_keys) - 1)
            same = (sorted_keys[at] == keys) & (keys != 0)
            positions = order[at[same]]
            ascending = np.argsort(positions)  # read in file order: the store may be on a disk
            rows = np.empty((len(positions), MODEL.dimensions), dtype=np.int8)
            rows[ascending] = stored[positions[ascending]]
            out[same] = rows
        return out

    def projection(self) -> Projection:
        """The store's projection, fitted on its first use: blocks of rows taken at random."""
        path = self.path / f"pca-{KEPT}.npz"
        if not path.exists():
            pick = np.random.default_rng(0)
            sizes = np.array([len(run[0]) for run in self._runs])
            blocks, size = FIT
            rows = []
            for r in pick.choice(len(self._runs), blocks, p=sizes / sizes.sum()):
                start = pick.integers(0, max(1, sizes[r] - size))
                rows.append(np.asarray(self._runs[r][2][start : start + size]))
            sample = np.concatenate(rows)
            Projection.fit(sample[sample.any(axis=1)]).save(path)
        return Projection.load(path)

    def add(self, keys: Any, vectors: Any) -> None:
        """Keep what a run embedded."""
        if not len(keys):
            return
        run = self.path / str(
            max((int(p.name) for p in self.path.iterdir() if p.name.isdigit()), default=-1) + 1
        )
        part = run.with_name(run.name + ".part")
        part.mkdir()
        np.save(part / "keys.npy", keys)
        np.save(part / "vectors.npy", vectors)
        part.replace(run)


def store() -> Store:
    return Store(
        cache_dir() / "embeddings" / f"{MODEL.repo.replace('/', '__')}@{MODEL.revision[:12]}"
    )


def embed(index: Path, gpu: bool) -> dict[str, Any]:
    """Give every snippet of the index its embedding: from the store if a run made it, else from
    the model, then kept in the store."""
    built = json.loads((index / "manifest.json").read_text(encoding="utf-8"))["built"]
    partitions = len(list((index / "partitions").iterdir()))
    kept = store()
    # When the run began, and what each partition took: a run interrupted (the GPU's driver kills
    # one every few hours) and run again counts all its work.
    begun = index / "dense.part.json"
    if not begun.exists():
        begun.write_text(json.dumps({"started": time.time(), "new": {}}), encoding="utf-8")
    run = json.loads(begun.read_text(encoding="utf-8"))

    @cache
    def encoder() -> Encoder:  # loaded only if some code is not in the store
        return Encoder(gpu)

    for n in range(partitions):  # first the store gets every function's embedding
        if str(n) not in run["new"]:
            run["new"][str(n)] = _embed(index, n, kept, encoder)
            begun.write_text(json.dumps(run), encoding="utf-8")
    kept = store()  # with this run's
    projection = kept.projection()
    projection.save(index / "pca.npz")
    for n in range(partitions):  # then each partition its rows, projected
        _project(index, n, kept, projection)
    about = {
        "index_built": built,
        "model": {"repo": MODEL.repo, "revision": MODEL.revision},
        "dimensions": KEPT,
        "embedded": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": source_commit(),
        "snippets": sum(len(_keys(index, n)) - 1 for n in range(partitions)),
        "new": sum(run["new"].values()),
        "seconds": round(time.time() - run["started"]),  # with the time it was interrupted
    }
    (index / "dense.json").write_text(json.dumps(about, indent=1) + "\n", encoding="utf-8")
    begun.unlink()
    return about


def _keys(index: Path, n: int) -> Any:
    """Partition n's store keys, by snippet id (0 for row 0 and ids no snippet has)."""
    db = search.connect(index, n)
    rows = (db.execute("SELECT max(id) FROM snippets").fetchone()[0] or 0) + 1
    keys = np.zeros(rows, dtype=np.uint64)
    for snippet, hash_ in db.execute("SELECT id, hash FROM snippets"):
        keys[snippet] = int.from_bytes(hash_[:8])
    db.close()
    return keys


def _embed(index: Path, n: int, kept: Store, encoder: Callable[[], Encoder]) -> int:
    """Embed partition n's code that the store does not have, and keep it there; return how
    much."""
    started = time.monotonic()
    here = index / "partitions" / str(n)
    keys = _keys(index, n)
    missing = np.flatnonzero(~kept.has(keys) & (keys != 0))  # the same after a crash: kept last
    names = {name: here / f"{name}.part.npy" for name in ("vectors", "done")}
    if not len(missing):
        for path in names.values():  # left by a run stopped right after keeping its vectors
            path.unlink(missing_ok=True)
        return 0
    if names["done"].exists():  # an interrupted run of this partition
        vectors, done = (np.load(path, mmap_mode="r+") for path in names.values())
    else:
        shape = (len(missing), MODEL.dimensions)
        vectors = np.lib.format.open_memmap(names["vectors"], "w+", np.int8, shape)
        done = np.lib.format.open_memmap(names["done"], "w+", np.bool_, (len(missing),))
    zdict = search.dictionary(index)
    db = search.connect(index, n)
    for start in range(0, len(missing), PART):
        rows = [r for r in range(start, min(start + PART, len(missing))) if not done[r]]
        if not rows:
            continue
        ids = [int(missing[r]) for r in rows]
        code = {
            snippet: zstd.decompress(blob, zstd_dict=zdict).decode()
            for snippet, blob in db.execute(
                "SELECT id, code FROM snippets WHERE id BETWEEN ? AND ?", (ids[0], ids[-1])
            )
        }
        vectors[rows] = np.rint(encoder()([code[i] for i in ids]) * 127).astype(np.int8)
        vectors.flush()
        done[rows] = True
        done.flush()
        rate = (start + len(rows)) / (time.monotonic() - started)
        print(f"[{n}: {start + len(rows)}/{len(missing)}] {rate:.0f}/s", flush=True)
    db.close()
    kept.add(keys[missing], np.asarray(vectors))
    del vectors, done  # close the maps before their files are removed
    for path in names.values():
        path.unlink()
    return len(missing)


def _project(index: Path, n: int, kept: Store, projection: Projection) -> None:
    """Write partition n's vectors from the store's, projected."""
    here = index / "partitions" / str(n)
    if (here / "vectors.npy").exists():
        return
    keys = _keys(index, n)
    part = here / "vectors.part.npy"
    vectors = np.lib.format.open_memmap(part, "w+", np.int8, (len(keys), KEPT))
    for start in range(0, len(keys), _BLOCK):
        vectors[start : start + _BLOCK] = projection(kept.take(keys[start : start + _BLOCK]))
    vectors.flush()
    del vectors  # closed before it is renamed
    part.replace(here / "vectors.npy")


def _files(gpu: bool) -> tuple[Path, Path]:
    """The model's ONNX file (half precision for a GPU) and tokenizer, downloaded once."""
    base = f"https://huggingface.co/{MODEL.repo}/resolve/{MODEL.revision}/"
    here = cache_dir() / "models" / f"{MODEL.repo.replace('/', '__')}@{MODEL.revision[:12]}"
    onnx, tokenizer = (
        download([base + path], sha256, here / path)
        for path, sha256 in (MODEL.onnx_fp16 if gpu else MODEL.onnx, MODEL.tokenizer)
    )
    return onnx, tokenizer
