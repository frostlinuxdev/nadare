# SPDX-License-Identifier: GPL-3.0-or-later
"""A username that is also a report label must not wipe the label out.

GitHub's runners log in as ``runner``: the case-insensitive username redaction turned
"Runner: ..." into "USER: ...". A player called ``runner``, ``kernel`` or ``session`` would
get the same broken report. The username is still removed everywhere else.
"""

from __future__ import annotations

import os

import pytest

from nadare.core import report


def _report(tmp_path, username, profile_extra=None):
    runner = tmp_path / "runners" / "GE-Proton11-7-x86_64"
    runner.mkdir(parents=True)
    profile = {
        "backend": "direct-wuwa",
        "title": "Wuthering Waves",
        "install_dir": str(tmp_path / "game"),
        "extra": {"runner": str(runner)},
        **(profile_extra or {}),
    }
    config = {"active_profile": "wuwa", "profiles": {"wuwa": profile}}
    return report.build_report(
        config,
        profile_name="wuwa",
        logs_path=tmp_path / "logs",
        profiles_path=tmp_path / "profiles",
        username=username,
        home="/home/" + username,
        hostname="hostbox",
        run_command=lambda *a, **k: "",
        doctor_text="ok",
        os_release_path=tmp_path / "missing-os-release",
    )


@pytest.mark.parametrize(
    ("username", "label"),
    [("runner", "Runner: "), ("kernel", "Kernel: "), ("session", "Session: "), ("gpu", "GPU: ")],
)
def test_a_username_that_is_a_label_keeps_the_label(tmp_path, username, label):
    text = _report(tmp_path, username)
    assert any(line.startswith(label) for line in text.splitlines()), text


def test_the_username_is_still_removed_from_values(tmp_path):
    text = _report(tmp_path, "runner", {"title": "runner's game", "args": "--who runner"})
    title = next(line for line in text.splitlines() if line.startswith("Title: "))
    assert "runner" not in title.lower()
    assert "USER" in title


def test_a_log_line_that_starts_with_the_username_is_still_redacted():
    out = report.redact(
        "Runner: GE\nrunner: hello from runner\n",
        username="runner",
        home="/home/runner",
        hostname="h",
        labels=("Runner",),
    )
    assert out.splitlines() == ["Runner: GE", "USER: hello from USER"]


def test_the_suite_runs_under_a_neutral_username():
    assert os.environ.get("USER") == "nadare-tester"
