import gzip
import json
from pathlib import Path

import pytest

from provgrep.distill import listed, name, sentence


def test_a_comment_gives_its_first_sentence_without_markers_or_tags() -> None:
    doc = """/**
     * Parses the header of an RTP packet. Returns its length.
     *
     * @param buf the packet
     */"""
    assert sentence(doc) == "Parses the header of an RTP packet"
    assert sentence("# Copyright (c) 2020 Acme, under the MIT License") is None
    assert sentence("// x = f(a, b); y = g(c); z = {d}") is None  # code, not words
    assert sentence("/// 启动加载") is None


def test_a_name_gives_its_words() -> None:
    assert name("AgentLauncher.writeAttachResult") == "agent launcher write attach result"
    assert name("main") is None


def test_texts_are_kept_for_the_inputs_they_were_listed_from(tmp_path: Path) -> None:
    staging, suites, out = tmp_path / "e7e7f9fcba59b564", tmp_path / "suites", tmp_path / "out"
    staging.mkdir()
    suites.mkdir()
    (suites / "a.json").write_text(json.dumps({"intents": [{"query": "parse a header"}]}))
    unit = {"symbol": "parseRtpHeader", "doc": "", "code": "", "hash": "0"}
    with gzip.open(staging / "r.jsonl.gz", "wt") as f:
        f.write(json.dumps({"path": "a.c", "units": [unit] * 100}) + "\n")
    first = listed(out, staging, suites)
    assert listed(out, staging, suites) == first
    (suites / "a.json").write_text(json.dumps({"intents": [{"query": "another question"}]}))
    with pytest.raises(ValueError, match="other inputs"):
        listed(out, staging, suites)
