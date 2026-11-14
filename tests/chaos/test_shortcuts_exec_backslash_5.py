# SPDX-License-Identifier: GPL-3.0-or-later
"""A backslash in the launcher path must be escaped twice for Exec.

The general string escape is applied before the Exec quoting rule: a literal
backslash is ``\\\\`` in the file, and then ``\\\\`` inside a quoted argument,
so four backslashes. Writing only two (``\\\\`` in the value, which the general
rule turns into one) leaves ``\\d`` inside the quotes and desktop-file-validate
reports "a quote which is not closed"; the shortcut cannot be launched.
"""

from __future__ import annotations

from nadare.core import shortcuts


def test_a_backslash_in_the_launcher_path_is_quoted_correctly():
    path = "/opt/na\\dare"
    text = shortcuts.desktop_entry("p", "T", [path])
    line = next(ln for ln in text.splitlines() if ln.startswith("Exec="))
    expected = 'Exec="' + "/opt/na" + "\\" * 4 + 'dare" launch -p p'
    print("launcher path:", repr(path))
    print("Exec:", repr(line))
    print("expected:", repr(expected))
    assert line == expected, (
        "the Exec line only doubles the backslash once; after the general value "
        "escape the quoted argument contains an invalid \\d escape"
    )
