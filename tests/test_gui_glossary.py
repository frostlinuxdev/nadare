# SPDX-License-Identifier: GPL-3.0-or-later
"""One word per thing in the GUI: retired wordings must not come back (b21 polish pass)."""

from __future__ import annotations

from pathlib import Path

GUI = Path(__file__).resolve().parents[1] / "src" / "nadare" / "gui"

#: Wording that was replaced, and what to use instead.
RETIRED = {
    "View logs": "Open logs",
    "Profile name": "Game name",
    "Delete unused old runners": "Delete unused runners",
    "Sign-in in progress": "Login in progress",
    '"File check"': "Check game files",
    "Add your own game...": "Add your own game… (one ellipsis character)",
    "backend output will appear": "Nadare's progress messages will appear here",
    "SET GAME EXE": "SET GAME FILE IN SETTINGS",
    "Stop and forget": "Stop this download and take it off this list.",
}


def test_retired_wording_stays_retired():
    found = []
    for path in sorted(GUI.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for old, new in RETIRED.items():
            if old in text:
                found.append(f"{path.name}: {old!r} (use: {new})")
    assert not found, "\n".join(found)


def test_every_button_that_opens_the_logs_says_open_logs():
    labels = []
    for path in sorted(GUI.glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "QPushButton(" in line and "logs" in line.lower():
                labels.append((path.name, line.strip()))
    assert labels, "the logs buttons moved: update this test"
    assert all("Open logs" in line for _, line in labels), labels
