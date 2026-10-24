from pathlib import Path

from provgrep.licenses import (
    Evidence,
    _combine,
    _resolve,
    _Scope,
    header_end,
    permissive,
    resolve_tree,
)


def test_header_ends_at_the_first_statement() -> None:
    py = '#!/usr/bin/env python\n# Copyright 2020 A\n"""Module.\n\nLicensed MIT.\n"""\nimport os\n'
    assert header_end(py, ".py") == 6
    c = "/* Copyright 2020 A\n * SPDX-License-Identifier: MIT\n */\n#include <stdio.h>\n"
    assert header_end(c, ".c") == 3  # `#include` is code in C, not a comment
    js = "#!/usr/bin/env node\n// SPDX-License-Identifier: MIT\nconst a = 1;\n"
    assert header_end(js, ".js") == 2
    assert header_end('x = 1\n"""not a header"""\n', ".py") == 0


def test_levels() -> None:
    mit_file = _Scope(0, "MIT", (Evidence("license_file", "LICENSE", "MIT"),))
    gpl_header = Evidence("header", "a.py", "GPL-3.0-or-later")
    own = _resolve(gpl_header, mit_file, None)
    assert (own.expression, own.level, own.conflict) == ("GPL-3.0-or-later", "A", True)

    both = _Scope(0, "MIT AND Apache-2.0", (Evidence("license_file", "LICENSE-MIT", "MIT"),))
    dual = _Scope(
        0, "MIT OR Apache-2.0", (Evidence("manifest", "Cargo.toml", "MIT OR Apache-2.0"),)
    )
    agreed = _resolve(None, both, dual)
    assert (agreed.expression, agreed.level) == ("MIT OR Apache-2.0", "B")

    vendored = _Scope(2, "BSD-3-Clause", (Evidence("license_file", "x/y/LICENSE", "BSD-3-Clause"),))
    root_manifest = _Scope(0, "MIT", (Evidence("manifest", "pyproject.toml", "MIT"),))
    deeper = _resolve(None, vendored, root_manifest)
    assert (deeper.expression, deeper.level, deeper.conflict) == ("BSD-3-Clause", "C", True)

    copying = _Scope(0, "LGPL-2.1-only", (Evidence("license_file", "COPYING", "LGPL-2.1-only"),))
    later = _Scope(0, "LGPL-2.1-or-later", (Evidence("manifest", "setup.py", "LGPL-2.1-or-later"),))
    gnu = _resolve(None, copying, later)
    assert (gnu.expression, gnu.level) == ("LGPL-2.1-or-later", "B")  # texts never say "or later"

    assert _resolve(None, None, None).level == "X"


def test_combine_flattens_and_drops_pointers_and_clas() -> None:
    pointer = "LicenseRef-scancode-unknown-license-reference"
    assert _combine(["MIT AND (MIT AND Apache-2.0)", f"{pointer} AND MIT"]) == "MIT AND Apache-2.0"
    assert _combine([pointer]) is None
    assert _combine(["MIT AND LicenseRef-scancode-generic-cla"]) == "MIT"


def test_permissive() -> None:
    assert permissive("MIT OR GPL-2.0-only")
    assert not permissive("MIT AND GPL-2.0-only")
    assert permissive("LicenseRef-scancode-public-domain")
    assert not permissive("MPL-2.0")
    assert not permissive(None)


def test_resolve_tree(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nlicense = "MIT"\n')
    (tmp_path / "LICENSE").write_text(
        "Permission is hereby granted, free of charge, to any person obtaining a copy\n"
        'of this software and associated documentation files (the "Software"), to deal\n'
        "in the Software without restriction, including without limitation the rights\n"
        "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
        "copies of the Software, and to permit persons to whom the Software is\n"
        "furnished to do so, subject to the following conditions:\n\n"
        "The above copyright notice and this permission notice shall be included in\n"
        "all copies or substantial portions of the Software.\n"
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "plain.py").write_text("def f():\n    return 'MIT or GPL'\n")
    (tmp_path / "pkg" / "gpl.py").write_text("# SPDX-License-Identifier: GPL-2.0-or-later\nx = 1\n")
    resolved = resolve_tree(tmp_path)
    assert resolved["pkg/plain.py"].level == "B"  # the string in the code is not its license
    assert resolved["pkg/plain.py"].expression == "MIT"
    assert resolved["pkg/gpl.py"].expression == "GPL-2.0-or-later"
    assert resolved["pkg/gpl.py"].level == "A"
