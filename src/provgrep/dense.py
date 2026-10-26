"""Order a search's lexical candidates by meaning: how close the embedding of each one's code is
to the query's.

One pinned model (MODEL) makes the embeddings, run by onnxruntime: on a GPU for the corpus
(`provgrep embed`, about a day for 21 million functions), on the CPU for a query.

dense/ in the index holds them: vectors.npy, row n for snippet n, as int8 (a unit vector times
127), and keys.npy, the snippet's id (the first 16 hex digits of its code's hash) for each row.
An index rebuilt from mostly the same code takes the vectors of the code it already had by key
and embeds only what is new; an interrupted run goes on where it stopped. dense.json says which
build of the index the rows are for: rows of another build would rank the wrong code.
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from provgrep import source_commit
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
_BLOCK = 1_000_000  # rows copied at a time


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

    def __init__(self, index: Path, built: str) -> None:
        self.about = json.loads((index / "dense" / "dense.json").read_text(encoding="utf-8"))
        if self.about["index_built"] != built:
            raise ValueError(
                f"dense/ is for the index built {self.about['index_built']}, not {built}:"
                " run provgrep embed"
            )
        self._vectors = np.load(index / "dense" / "vectors.npy", mmap_mode="r")

    def similarity(self, query: Any, snippets: list[int]) -> Any:
        """The cosine of each snippet's code with the query (0 for one not embedded)."""
        rows = self._vectors[snippets].astype(np.float32)
        return rows @ query / np.maximum(np.linalg.norm(rows, axis=1), 1e-9)


def embed(index: Path, gpu: bool) -> dict[str, Any]:
    """Embed the code of every snippet of the index that dense/ does not have yet."""
    started = time.monotonic()
    built = json.loads((index / "manifest.json").read_text(encoding="utf-8"))["built"]
    out = index / "dense"
    out.mkdir(exist_ok=True)
    db = sqlite3.connect(f"{(index / 'records.sqlite').resolve().as_uri()}?mode=ro", uri=True)
    rows = (db.execute("SELECT max(id) FROM snippets").fetchone()[0] or 0) + 1
    names = {name: out / f"{name}.part.npy" for name in ("vectors", "keys", "done")}
    # Written once the parts are ready: when the run began and how many rows it took from the
    # last one, for a run interrupted (the GPU's driver kills one every few hours) and resumed.
    begun = out / "run.part.json"
    if begun.exists():
        vectors, keys, done = (np.load(path, mmap_mode="r+") for path in names.values())
    else:
        shape = (rows, MODEL.dimensions)
        vectors = np.lib.format.open_memmap(names["vectors"], "w+", np.int8, shape)
        keys = np.lib.format.open_memmap(names["keys"], "w+", np.uint64, (rows,))
        done = np.lib.format.open_memmap(names["done"], "w+", np.bool_, (rows,))
        for snippet, hash_ in db.execute("SELECT id, hash FROM snippets"):
            keys[snippet] = int(hash_[:16], 16)
        if (out / "vectors.npy").exists():
            _reuse(out, vectors, keys, done)
        vectors.flush()  # not in a loop, whose variable would keep a map open after `del`
        keys.flush()
        done.flush()
        run = {"started": time.time(), "reused": int(np.count_nonzero(done))}
        begun.write_text(json.dumps(run), encoding="utf-8")
    run = json.loads(begun.read_text(encoding="utf-8"))
    encoder = Encoder(gpu)
    new = 0
    for start in range(1, rows, PART):
        wanted = [s for s in range(start, min(start + PART, rows)) if not done[s]]
        if not wanted:
            continue
        code = dict(
            db.execute(
                "SELECT id, code FROM snippets WHERE id BETWEEN ? AND ?", (wanted[0], wanted[-1])
            )
        )
        wanted = [s for s in wanted if s in code]  # ids no snippet has
        vectors[wanted] = np.rint(encoder([code[s] for s in wanted]) * 127).astype(np.int8)
        vectors.flush()
        done[wanted] = True
        done.flush()
        new += len(wanted)
        rate = new / (time.monotonic() - started)
        print(
            f"[{min(start + PART, rows) - 1}/{rows - 1}] {new} embedded, {rate:.0f}/s", flush=True
        )
    embedded = int(np.count_nonzero(done)) - run["reused"]
    del vectors, keys, done  # close the maps before their files are renamed
    names["vectors"].replace(out / "vectors.npy")
    names["keys"].replace(out / "keys.npy")
    names["done"].unlink()
    begun.unlink()
    about = {
        "index_built": built,
        "model": {"repo": MODEL.repo, "revision": MODEL.revision},
        "embedded": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": source_commit(),
        "snippets": rows - 1,
        "new": embedded,
        "seconds": round(time.time() - run["started"]),  # with the time it was interrupted
    }
    (out / "dense.json").write_text(json.dumps(about, indent=1) + "\n", encoding="utf-8")
    return about


def _reuse(out: Path, vectors: Any, keys: Any, done: Any) -> None:
    """Copy the rows of the last run whose code this build still has, found by key."""
    old_keys = np.load(out / "keys.npy")
    old_vectors = np.load(out / "vectors.npy", mmap_mode="r")
    order = np.argsort(old_keys)
    for start in range(0, len(keys), _BLOCK):
        block = np.asarray(keys[start : start + _BLOCK])
        rows = order[np.minimum(np.searchsorted(old_keys, block, sorter=order), len(order) - 1)]
        same = (old_keys[rows] == block) & (block != 0)  # 0: row 0, or an id no snippet has
        vectors[start : start + _BLOCK][same] = old_vectors[rows[same]]
        done[start : start + _BLOCK] = same


def _files(gpu: bool) -> tuple[Path, Path]:
    """The model's ONNX file (half precision for a GPU) and tokenizer, downloaded once."""
    base = f"https://huggingface.co/{MODEL.repo}/resolve/{MODEL.revision}/"
    here = cache_dir() / "models" / f"{MODEL.repo.replace('/', '__')}@{MODEL.revision[:12]}"
    onnx, tokenizer = (
        download([base + path], sha256, here / path)
        for path, sha256 in (MODEL.onnx_fp16 if gpu else MODEL.onnx, MODEL.tokenizer)
    )
    return onnx, tokenizer
