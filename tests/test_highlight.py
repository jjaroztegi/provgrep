from provgrep import highlight


def test_runs_colour_the_syntax_and_keep_every_character() -> None:
    code = 'def f(x=None):\n    """doc"""\n    return len(x) + 0x1F  # n\n'
    runs = highlight.runs("pkg/a.py", code)
    assert "".join(text for _, text in runs) == code
    coloured = {text: cls for cls, text in runs if cls}
    assert coloured["def"] == "k"
    assert coloured['"""doc"""'] == "s"
    assert coloured["0x1F"] == "n"
    assert coloured["# n"] == "c"
    assert "len" not in coloured  # calls stay plain, as the theme leaves them


def test_a_method_cut_from_its_class_is_parsed_inside_one() -> None:
    code = "private delay(): number {\n  return this.base * 2;\n}"
    runs = highlight.runs("src/retry.ts", code)
    assert "".join(text for _, text in runs) == code
    assert ("t", "number") in runs
    assert ("k", "this") in runs


def test_unknown_files_are_plain() -> None:
    assert highlight.runs("notes.txt", "if x") == (("", "if x"),)
