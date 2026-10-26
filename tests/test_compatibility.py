from provgrep.compatibility import PROJECTS, project, verdict


def test_verdicts_are_osadls_answers() -> None:
    assert [project(p) for p in PROJECTS] == list(PROJECTS)  # all are rows of the matrix
    assert verdict("GPL-2.0-only", [["Apache-2.0"]]).answer == "no"
    depends = verdict("GPL-2.0-or-later", [["Apache-2.0"]])
    assert depends.answer == "conditional"
    assert "GPL-3.0-or-later" in depends.reasons[0]  # OSADL's explanation says how
    assert verdict("GPL-3.0-or-later", [["MPL-2.0"]]).answer == "yes"
    assert verdict("Apache-2.0", [["GPL-2.0-only WITH Classpath-exception-2.0"]]).answer == "no"


def test_what_the_matrix_lacks_is_unknown() -> None:
    assert verdict("MIT", [["CC0-1.0"]]).answer == "unknown"
    # An exception the matrix lacks takes its license's answer, but cannot make it a no.
    assert verdict("GPL-2.0-only", [["GPL-2.0-only WITH Linux-syscall-note"]]).answer == "yes"
    assert verdict("GPL-2.0-only", [["Apache-2.0 WITH LLVM-exception"]]).answer == "unknown"
    assert verdict("MIT", []).reasons == ["no license found"]


def test_the_best_way_to_comply_decides() -> None:
    either = verdict("MIT", [["GPL-3.0-only"], ["BSD-2-Clause", "MIT"]])
    assert (either.answer, either.licenses) == ("yes", ["BSD-2-Clause", "MIT"])
    assert verdict("MIT", [["MIT", "GPL-3.0-only"]]).answer == "no"  # all of them apply
