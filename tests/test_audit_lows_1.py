# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit lows #590, #591, #596, #597 and #620."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from nadare.backends.czn.backend import WEBVIEW2_INDEX_MAX_HASHES, CznBackend
from nadare.core import indexfetch, shortcuts, umu

ROOT = Path(__file__).resolve().parents[1]
A = "https://a.example/index/"


@pytest.fixture
def index(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("NADARE_INDEX_BASE", A)

    def serve(document):
        payload = json.dumps(document).encode()
        monkeypatch.setattr(indexfetch, "open_bytes", lambda *_a, **_k: payload)

    return serve


# ---- #597: junk in front of the real hashes must not hide them --------------------------


def test_webview2_junk_entries_in_front_do_not_hide_the_real_hash(index):
    good = hashlib.sha256(b"real").hexdigest()
    index({"sha256": ["junk"] * 20 + [good]})
    assert CznBackend._webview2_index_pins() == {good}


def test_webview2_valid_hashes_are_still_capped(index):
    total = WEBVIEW2_INDEX_MAX_HASHES + 5
    many = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(total)]
    index({"sha256": many})
    assert CznBackend._webview2_index_pins() == set(many[:WEBVIEW2_INDEX_MAX_HASHES])


# ---- #620: a trailing backslash must not break the .desktop file -------------------------


def _name_line(entry: str) -> str:
    return next(line for line in entry.splitlines() if line.startswith("Name="))


def test_a_trailing_backslash_in_the_title_is_escaped():
    entry = shortcuts.desktop_entry("p", "Foo\\", ["nadare"])
    assert _name_line(entry) == "Name=Foo\\\\"


def test_backslashes_in_the_profile_name_are_escaped_too():
    entry = shortcuts.desktop_entry("pro\\file", "T", ["nadare"])
    assert "X-Nadare-Profile=pro\\\\file" in entry


def test_control_characters_are_still_flattened():
    entry = shortcuts.desktop_entry("p", "a\nb\tc", ["nadare"])
    assert _name_line(entry) == "Name=a b c"


def test_a_plain_title_is_untouched():
    assert _name_line(shortcuts.desktop_entry("p", "Punishing: Gray Raven", ["nadare"])) == (
        "Name=Punishing: Gray Raven"
    )


# ---- #596/#1341: without that interpreter, Nadare's own Python runs the zipapp ----------


def _fake_managed(tmp_path, monkeypatch):
    target = tmp_path / "umu-run"
    target.write_text("#!/usr/bin/python3\n")
    target.chmod(0o755)
    monkeypatch.setattr(umu, "managed_path", lambda: target)
    return target


def test_managed_umu_runs_without_its_shebang_interpreter(tmp_path, monkeypatch):
    target = _fake_managed(tmp_path, monkeypatch)
    monkeypatch.setattr(umu, "SHEBANG_PYTHON", str(tmp_path / "no-such-python3"))
    monkeypatch.setattr(umu.shutil, "which", lambda name, *_a, **_k: "/home/u/.pyenv/shims/python3")
    # The fallback (`nadare --umu-run`) does not need the shebang interpreter (#1341 item 3).
    assert umu.managed() == target
    command = umu.command()
    assert umu.run_argv(str(target), ["game.exe"])[: len(command)] == command


# ---- #590: the Yostar workflow template pins its actions by commit -----------------------


def test_the_yostar_workflow_pins_actions_by_sha():
    text = (ROOT / "ci/yostar-manifests/workflow.yml").read_text(encoding="utf-8")
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, re.M)
    assert uses, "the template should use actions"
    for ref in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), f"not pinned by commit: {ref}"
