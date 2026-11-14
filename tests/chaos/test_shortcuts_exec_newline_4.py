# SPDX-License-Identifier: GPL-3.0-or-later
"""A launcher path with a control character must not be silently changed.

``_one_line`` flattens newline/tab/CR to spaces. For the ``Name`` key that is a
display choice, but the same helper is used for ``Exec`` arguments: the entry
then starts a *different* (usually nonexistent) path and the shortcut does
nothing. The desktop-entry value spelling is ``\\n``/``\\t``/``\\r``.
"""

from __future__ import annotations

import pytest

from nadare.core import shortcuts


@pytest.mark.parametrize(
    ("path", "escaped"),
    [
        ("/opt/na\ndare", "\\n"),
        ("/opt/na\tdare", "\\t"),
        ("/opt/na\rdare", "\\r"),
    ],
)
def test_a_control_character_in_the_launcher_path_is_encoded_not_flattened(path, escaped):
    try:
        text = shortcuts.desktop_entry("p", "T", [path])
    except (shortcuts.ShortcutError, ValueError):
        return  # refusing with a clear error is acceptable

    line = next(ln for ln in text.splitlines() if ln.startswith("Exec="))
    expected = 'Exec="/opt/na' + escaped + 'dare" launch -p p'
    print("launcher path:", repr(path))
    print("Exec:", repr(line))
    print("expected:", repr(expected))
    assert line == expected, (
        "the Exec line silently points at a different launcher path because the "
        "control character was replaced with a space"
    )
