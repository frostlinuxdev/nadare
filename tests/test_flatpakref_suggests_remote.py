# SPDX-License-Identifier: GPL-3.0-or-later
"""The published .flatpakref names its remote, so software centres show Nadare's listing.

Without ``SuggestRemoteName`` Flatpak adds a hidden ``nadare-origin`` remote marked
no-enumerate: its AppStream (icon, version, screenshots) is never fetched and the store page
is bare. With it, GNOME Software added an enumerable ``nadare`` remote and showed the listing
(Ubuntu 26.04, 2026-11-14). Mint's Software Manager ignores the key (always ``-origin``).
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "flatpak" / "build_repo.sh"


def _flatpakref_heredoc() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r'cat > "\$repo/nadare\.flatpakref" <<EOF\n(.*?)\nEOF\n', text, re.S)
    assert match, "build_repo.sh no longer writes nadare.flatpakref the expected way"
    return match.group(1)


def test_the_flatpakref_suggests_a_remote_name():
    lines = _flatpakref_heredoc().splitlines()
    assert "SuggestRemoteName=nadare" in lines


def test_the_flatpakref_keeps_its_required_keys():
    keys = {line.split("=", 1)[0] for line in _flatpakref_heredoc().splitlines() if "=" in line}
    assert {"Name", "Branch", "Url", "RuntimeRepo", "IsRuntime", "GPGKey"} <= keys
