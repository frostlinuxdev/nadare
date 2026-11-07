# SPDX-License-Identifier: GPL-3.0-or-later
"""The .deb copyright text is written as text, not run as shell commands (#870)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_deb.sh"


def _heredoc() -> str:
    text = SCRIPT.read_text()
    match = re.search(
        r'cat > "\$pkg/usr/share/doc/nadare/copyright" <<EOF\n(.*?)\nEOF\n', text, re.S
    )
    assert match, "the copyright heredoc is gone from build_deb.sh"
    return match.group(1)


def test_rendering_the_copyright_text_runs_nothing_and_keeps_every_word():
    body = _heredoc()
    done = subprocess.run(
        ["bash", "-c", 'version=1.2.3\ncat <<EOF\n' + body + "\nEOF\n"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.stderr == ""  # no "command not found"
    assert done.returncode == 0
    assert "Nadare 1.2.3" in done.stdout
    assert done.stdout.count("GPL-3.0-or-later") == 1
    assert done.stdout.count("GPL-3.0-only") == 2  # the licence line and the whole-work line


def test_there_are_no_unescaped_backticks_in_the_unquoted_heredoc():
    body = _heredoc()
    assert not re.search(r"(?<!\\)`", body)
