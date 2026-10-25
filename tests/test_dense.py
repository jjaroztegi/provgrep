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

    manifest.write_text(json.dumps({"built": "second"}))  # a rebuild of the same code
    with pytest.raises(ValueError, match="run provgrep embed"):
        search.Index(index.path)
    again = dense.embed(index.path, gpu=False)
    assert (again["new"], again["index_built"]) == (0, "second")
    assert (np.load(index.path / "dense" / "vectors.npy") == vectors).all()
