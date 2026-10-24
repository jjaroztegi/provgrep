from provgrep.debian import candidates, dep5, spdx_expression, stanza_for

COPYRIGHT = """\
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: demo

Files: *
Copyright: 2020 A
License: Expat

Files: src/vendor/*
 src/extra?.py
Copyright: 2019 B
License: GPL-2+ or Artistic-2.0
Comment: two patterns, the second on a continuation line

Files: debian/*
Copyright: 2021 C
License: GPL-2+

License: Expat
 Permission is hereby granted...
"""


def test_last_matching_stanza_wins() -> None:
    stanzas = dep5(COPYRIGHT)
    assert stanzas is not None
    assert [s.license for s in stanzas] == ["Expat", "GPL-2+ or Artistic-2.0", "GPL-2+"]

    def license_of(path: str) -> str | None:
        stanza = stanza_for(stanzas, path)
        return stanza.license if stanza else None

    assert license_of("setup.py") == "Expat"
    assert license_of("src/vendor/deep/x.py") == "GPL-2+ or Artistic-2.0"  # `*` crosses `/`
    assert license_of("src/extra1.py") == "GPL-2+ or Artistic-2.0"
    assert license_of("src/extra10.py") == "Expat"  # `?` is one character
    assert dep5("Format: something else\n\nFiles: *\nLicense: MIT\n") is None


def test_debian_names_to_spdx() -> None:
    assert spdx_expression("Expat") == "MIT"
    assert spdx_expression("GPL-2+ or Artistic-2.0") == "GPL-2.0-or-later OR Artistic-2.0"
    assert spdx_expression("LGPL-2.1+") == "LGPL-2.1-or-later"
    assert spdx_expression("GPL-3") == "GPL-3.0-only"
    assert spdx_expression("BSD-3-clause") == "BSD-3-Clause"
    assert spdx_expression("MIT or GPL-2+, and BSD-2-clause") == (
        "(MIT OR GPL-2.0-or-later) AND (BSD-2-Clause)"
    )
    assert spdx_expression("Apache") is None  # which Apache?
    assert spdx_expression("GPL-2+ with OpenSSL exception") is None


def test_candidates_take_python_packages_from_github_with_one_tarball() -> None:
    def source(name: str, binary: str, homepage: str, origs: list[str]) -> str:
        files = [f" {'0' * 64} 10 {f}" for f in [*origs, f"{name}_1-1.debian.tar.xz"]]
        return "\n".join(
            [
                f"Package: {name}",
                f"Binary: {binary}",
                "Version: 1-1",
                "Format: 3.0 (quilt)",
                "Checksums-Sha256:",
                *files,
                f"Homepage: {homepage}",
                f"Directory: pool/main/{name[0]}/{name}",
                "",
            ]
        )

    sources = "\n".join(
        [
            source("python-a", "python3-a", "https://github.com/x/a", ["python-a_1.orig.tar.gz"]),
            source("b", "b-utils", "https://github.com/x/b", ["b_1.orig.tar.gz"]),
            source("python-c", "python3-c", "https://example.org", ["python-c_1.orig.tar.gz"]),
            source(
                "python-d",
                "python3-d",
                "https://github.com/x/d",
                ["python-d_1.orig.tar.gz", "python-d_1.orig-docs.tar.gz"],
            ),
        ]
    )
    assert [p.name for p in candidates(sources)] == ["python-a"]
