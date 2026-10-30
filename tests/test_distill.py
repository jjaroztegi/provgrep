from provgrep.distill import name, sentence


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
