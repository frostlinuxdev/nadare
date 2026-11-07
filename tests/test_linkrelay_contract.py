# SPDX-License-Identifier: GPL-3.0-or-later
"""The link relay depends on what the Steam Runtime's ``steam-runtime-urlopen`` writes (#804).

That behaviour was observed, not documented, so this runs the REAL helper against the REAL relay.
It needs a downloaded runtime, so it skips on a machine that has none (CI); point
``NADARE_RUNTIME_URLOPEN`` at a ``steam-runtime-urlopen`` to run it anywhere. If Valve ever changes
the pipe format, this fails here instead of silently breaking players' sign-in buttons.
"""

from __future__ import annotations

import glob
import os
import subprocess
import time
from pathlib import Path

import pytest

from nadare.core.linkrelay import LinkRelay


def _helper() -> str | None:
    explicit = os.environ.get("NADARE_RUNTIME_URLOPEN")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    pattern = os.path.join(glob.escape(data), "umu", "*", "*_platform_*", "files", "bin")
    for folder in sorted(glob.glob(pattern), reverse=True):
        candidate = os.path.join(folder, "steam-runtime-urlopen")
        if os.access(candidate, os.X_OK):
            return candidate
    return None


HELPER = _helper()
pytestmark = pytest.mark.skipif(
    HELPER is None, reason="no Steam Runtime on this machine (set NADARE_RUNTIME_URLOPEN)"
)

LINKS = [
    "https://accounts.example.com/login?return=game",
    "https://accounts.google.com/o/oauth2/v2/auth?client_id=1&redirect_uri=http%3A%2F%2Flocalhost%3A8080&state=a%20b",
    "http://127.0.0.1:53682/?code=abc#frag",
]


@pytest.mark.parametrize("url", LINKS)
def test_the_real_helper_writes_what_the_relay_reads(tmp_path, url):
    opened: list[str] = []
    relay = LinkRelay(tmp_path, opener=opened.append, min_gap=0.0)
    pipe = relay.start()
    home = tmp_path / "home"
    (home / ".steam").mkdir(parents=True)
    (home / ".steam" / "steam.pipe").symlink_to(pipe)
    try:
        done = subprocess.run(
            [HELPER, url],
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        end = time.monotonic() + 5
        while time.monotonic() < end and not opened:
            time.sleep(0.05)
    finally:
        relay.stop()
    assert opened == [url], (done.returncode, done.stderr[:300])
    assert done.returncode == 0


def test_the_real_helper_with_a_refused_link_opens_nothing(tmp_path):
    """The helper writes whatever it is given; the relay must still refuse it."""
    opened: list[str] = []
    relay = LinkRelay(tmp_path, opener=opened.append, min_gap=0.0)
    pipe = relay.start()
    home = Path(tmp_path / "home")
    (home / ".steam").mkdir(parents=True)
    (home / ".steam" / "steam.pipe").symlink_to(pipe)
    try:
        subprocess.run(
            [HELPER, "file:///etc/passwd"],
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            timeout=20,
            check=False,
        )
        time.sleep(0.5)
    finally:
        relay.stop()
    assert opened == []
