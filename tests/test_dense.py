import json
from typing import Any

import numpy as np
import pytest

from provgrep import dense, search


class Words:
    """An encoder whose vectors are the words of a text: close when they share words."""

    def __init__(self, gpu: bool = False) -> None:
        pass

    def __call__(self, texts: list[str]) -> Any:
        vectors = np.zeros((len(texts), dense.MODEL.dimensions), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in search.terms(text):
                vectors[row, sum(map(ord, word)) % dense.MODEL.dimensions] += 1
        return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)

    def query(self, text: str) -> Any:
        return self([text])[0]


def test_embeddings_order_results_and_outlive_a_rebuild(
    index: search.Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dense, "Encoder", Words)
    manifest = index.path / "manifest.json"
    manifest.write_text(json.dumps({"built": "first"}))
    assert dense.embed(index.path, gpu=False)["new"] == 4
    vectors = np.load(index.path / "dense" / "vectors.npy")
    assert vectors.shape == (5, dense.MODEL.dimensions)  # row n is snippet n; 0 is none
    assert not vectors[0].any()
    assert all(vectors[n].any() for n in range(1, 5))

    ordered = search.Index(index.path)
    (first, *_) = ordered.search("xrender fill rectangle color", licenses="any").results
    assert first.location.path in ("src/fill.c", "fill_copy.c")
    assert 0 < first.score <= 1  # a cosine, not BM25
    del ordered  # it maps vectors.npy, which Windows would not let the next run replace

    manifest.write_text(json.dumps({"built": "second"}))  # a rebuild of the same code
    with pytest.raises(ValueError, match="run provgrep embed"):
        search.Index(index.path)
    again = dense.embed(index.path, gpu=False)
    assert (again["new"], again["index_built"]) == (0, "second")
    assert (np.load(index.path / "dense" / "vectors.npy") == vectors).all()


class Dies(Words):
    """Words, but the second batch kills the run, as the GPU's driver does now and then."""

    calls = 0

    def __call__(self, texts: list[str]) -> Any:
        Dies.calls += 1
        if Dies.calls == 2:
            raise RuntimeError("illegal instruction")
        return super().__call__(texts)


def test_a_resumed_run_counts_all_its_work(
    index: search.Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dense, "Encoder", Dies)
    monkeypatch.setattr(dense, "PART", 2)  # snippets 1-2, then 3-4
    (index.path / "manifest.json").write_text(json.dumps({"built": "first"}))
    with pytest.raises(RuntimeError):
        dense.embed(index.path, gpu=False)
    assert dense.embed(index.path, gpu=False)["new"] == 4  # not the 2 of the last start
    assert sorted(p.name for p in (index.path / "dense").iterdir()) == [
        "dense.json",
        "keys.npy",
        "vectors.npy",
    ]
