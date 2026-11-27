# SPDX-License-Identifier: GPL-3.0-or-later
"""#1299: after umu updates its runtime, the Sandbox says so and the player can trust the copy.

Nadare records the runtime's ``srt-bwrap`` the first time the Sandbox starts and then runs only
that exact file (#871: the folder is writable by games). umu updates its runtime every few weeks,
which changes the file (two test PCs, checked 2026-11-16: pins from 11-07/11-09,
runtime 0.20261110). Where the system's bubblewrap is blocked (Ubuntu 24.04+), the Sandbox then
had no copy left: on by default the game played unsandboxed with one log line, switched on it
refused with "the system blocks it". Now the reason is named, and start-up asks once whether to
trust the new copy (never while a runtime process runs).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from nadare.core import gamesandbox, sandbox


def _copy(tmp_path: Path, rt: str = "steamrt3", body: str = "v1") -> Path:
    copy = (
        tmp_path / "umu" / rt / "pressure-vessel" / "libexec" / "steam-runtime-tools-0"
        / "srt-bwrap"
    )
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(f"#!/bin/sh\n# {body}\nexit 0\n")
    copy.chmod(0o755)
    return copy


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Runtime copies on disk, a working runtime copy, no system bubblewrap, nothing running."""
    on_disk: list[str] = []
    monkeypatch.setattr(gamesandbox, "runtime_copies", lambda: list(on_disk))
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(gamesandbox, "runtime_in_use", lambda copies=None: False)
    gamesandbox._READY_CACHE.clear()
    return on_disk


def _record(*copies: Path) -> None:
    path = gamesandbox.pins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({str(c): _sha(c) for c in copies}))


def _updated(tmp_path, rig) -> Path:
    """A copy recorded at the first start, then rewritten by a umu runtime update."""
    copy = _copy(tmp_path)
    rig.append(str(copy))
    _record(copy)
    copy.write_text("#!/bin/sh\n# v2, pressure-vessel 0.20261110\nexit 0\n")
    return copy


# ---- what is untrusted ---------------------------------------------------------------------
def test_a_rewritten_recorded_copy_is_untrusted(tmp_path, rig):
    copy = _updated(tmp_path, rig)
    assert gamesandbox.untrusted_runtime_copies() == [str(copy)]


def test_a_new_copy_next_to_a_record_is_untrusted(tmp_path, rig):
    old = _copy(tmp_path, "steamrt3")
    rig.append(str(old))
    _record(old)
    new = _copy(tmp_path, "steamrt4")
    rig.insert(0, str(new))
    assert gamesandbox.untrusted_runtime_copies() == [str(new)]


def test_nothing_recorded_yet_is_not_untrusted(tmp_path, rig):
    rig.append(str(_copy(tmp_path)))
    assert gamesandbox.untrusted_runtime_copies() == [], "the first Sandbox start records it"
    gamesandbox.pins_path().parent.mkdir(parents=True, exist_ok=True)
    gamesandbox.pins_path().write_text("{}")
    assert gamesandbox.untrusted_runtime_copies() == [], "an empty record heals the same way"
    assert gamesandbox.pins_path().read_text() == "{}", "looking records nothing"


# ---- locate and the question ----------------------------------------------------------------
def test_locate_names_the_update(tmp_path, rig):
    copy = _updated(tmp_path, rig)
    located = gamesandbox.locate(record=False)
    assert located.binary is None and located.found
    assert located.untrusted == (str(copy),)
    assert "updated" in located.why and "trust" in located.why.lower()


def test_the_question_is_asked_only_when_no_bubblewrap_is_left(tmp_path, rig, monkeypatch):
    copy = _updated(tmp_path, rig)
    assert gamesandbox.trust_question() == [str(copy)]
    # A working system bubblewrap: the Sandbox runs anyway, nobody is asked.
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda *a, **k: "/usr/bin/bwrap")
    assert gamesandbox.trust_question() == []


def test_the_question_is_not_asked_in_the_flatpak_or_without_a_change(tmp_path, rig, monkeypatch):
    copy = _copy(tmp_path)
    rig.append(str(copy))
    _record(copy)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (False, "no"))
    assert gamesandbox.trust_question() == [], "nothing changed: not a trust question"
    copy.write_text("changed")
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    assert gamesandbox.trust_question() == []


# ---- trusting -------------------------------------------------------------------------------
def test_trusting_records_the_new_copy_and_the_sandbox_uses_it(tmp_path, rig):
    copy = _updated(tmp_path, rig)
    other = _copy(tmp_path, "steamrt4-arm64", body="arm")
    rig.append(str(other))
    pins = json.loads(gamesandbox.pins_path().read_text())
    pins[str(other)] = _sha(other)
    gamesandbox.pins_path().write_text(json.dumps(pins))

    assert gamesandbox.trust_runtime_copies([str(copy)]) == [str(copy)]
    pins = json.loads(gamesandbox.pins_path().read_text())
    assert pins == {str(copy): _sha(copy), str(other): _sha(other)}, "other records are kept"
    located = gamesandbox.locate(record=False)
    assert located.binary == str(copy) and located.untrusted == ()
    assert gamesandbox.untrusted_runtime_copies() == []


def test_only_runtime_copies_on_disk_can_be_trusted(tmp_path, rig):
    copy = _updated(tmp_path, rig)
    stray = tmp_path / "elsewhere" / "srt-bwrap"
    stray.parent.mkdir()
    stray.write_text("x")
    before = gamesandbox.pins_path().read_text()
    assert gamesandbox.trust_runtime_copies([str(stray)]) == []
    assert gamesandbox.pins_path().read_text() == before
    assert gamesandbox.trust_runtime_copies([str(stray), str(copy)]) == [str(copy)]
    assert str(stray) not in json.loads(gamesandbox.pins_path().read_text())


def test_trusting_is_refused_while_a_runtime_process_runs(tmp_path, rig, monkeypatch):
    copy = _updated(tmp_path, rig)
    monkeypatch.setattr(gamesandbox, "runtime_in_use", lambda copies=None: True)
    before = gamesandbox.pins_path().read_text()
    with pytest.raises(gamesandbox.RuntimeBusy):
        gamesandbox.trust_runtime_copies([str(copy)])
    assert gamesandbox.pins_path().read_text() == before


def test_runtime_in_use_sees_a_process_started_from_the_runtime(tmp_path):
    copy = _copy(tmp_path)
    proc = tmp_path / "proc"

    def pid(n: int, *argv: str) -> None:
        d = proc / str(n)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "environ").write_bytes(b"")

    pid(10, "/usr/bin/bash")
    assert gamesandbox.runtime_in_use([str(copy)], proc_root=proc) is False
    runtime_dir = copy.parents[3]  # umu/steamrt3
    pid(11, str(runtime_dir / "pressure-vessel" / "bin" / "pv-adverb"), "--x")
    assert gamesandbox.runtime_in_use([str(copy)], proc_root=proc) is True


def test_a_sandboxed_game_counts_as_in_use(tmp_path):
    copy = _copy(tmp_path)
    proc = tmp_path / "proc"
    d = proc / "12"
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(f"{copy}\0--die-with-parent\0".encode())
    (d / "environ").write_bytes(b"")
    assert gamesandbox.runtime_in_use([str(copy)], proc_root=proc) is True


# ---- the launch messages ----------------------------------------------------------------------
def test_explicit_sandbox_names_the_update_not_ubuntu(tmp_path, rig, monkeypatch):
    from _backend_helpers import make_context, write_exe

    from nadare.backends import wine as generic_wine

    _updated(tmp_path, rig)
    game = tmp_path / "games" / "Thing"
    exe = write_exe(game / "thing.exe", "MZ")
    ctx = make_context(tmp_path, "wine", {
        "prefix": str(tmp_path / "pfx"), "install_dir": str(game), "exe_name": str(exe),
        "wine_binary": "/usr/bin/wine", "secure_mode": True})
    backend = generic_wine.WineBackend(ctx)
    monkeypatch.setattr(generic_wine.sandbox, "in_flatpak", lambda: False)
    with pytest.raises(generic_wine.BackendError) as caught:
        backend.build()
    text = str(caught.value)
    assert "updated" in text and "Trust" in text
    assert "Ubuntu" not in text


def test_on_by_default_says_why_it_runs_without(tmp_path, rig, monkeypatch):
    from _backend_helpers import make_context, write_exe

    from nadare.backends import wine as generic_wine
    from nadare.core import gamesandbox as gs

    monkeypatch.setattr(gs, "DEFAULT_ON", True)
    _updated(tmp_path, rig)
    game = tmp_path / "games" / "Thing"
    exe = write_exe(game / "thing.exe", "MZ")
    events = []
    ctx = make_context(tmp_path, "wine", {
        "prefix": str(tmp_path / "pfx"), "install_dir": str(game), "exe_name": str(exe),
        "wine_binary": "/usr/bin/wine"})
    backend = generic_wine.WineBackend(dataclasses.replace(ctx, emit=events.append))
    monkeypatch.setattr(backend, "secure_mode_enabled", lambda: True)
    monkeypatch.setattr(generic_wine.sandbox, "in_flatpak", lambda: False)
    argv, _env, _prefix, _target = backend.build()
    assert Path(argv[0]).name != "srt-bwrap"
    said = " ".join(getattr(e, "message", str(e)) for e in events)
    assert "updated" in said and "Trust" in said
    # Not the generic "cannot start" sentence; a bare "doctor" also matches tmp paths.
    assert "runs without the Sandbox" in said and "`nadare doctor`" not in said, "its own sentence"


# ---- start-up -----------------------------------------------------------------------------
class _Window:
    def __init__(self, profiles):
        from types import SimpleNamespace

        self.config = SimpleNamespace(notice=None, profiles=profiles, data={})
        self.emitted: list = []
        self.sandbox_trust_found = SimpleNamespace(emit=self.emitted.append)
        self.resynced = 0
        self._sandbox_ready_answer = False

    def _sync_sandbox(self, _profile):
        self.resynced += 1

    _current_profile = "g"


def _profiles(on=True):
    return {"g": {"backend": "direct-bd2", "secure_mode": on}}


def test_startup_asks_and_yes_trusts(tmp_path, rig, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from nadare.gui.main_window_parts import cleanup

    copy = _updated(tmp_path, rig)
    win = _Window(_profiles())
    cleanup.CleanupMixin._run_sandbox_trust_check(win, _profiles())
    assert win.emitted == [[str(copy)]]
    asked = []

    def question(*args, **kwargs):
        asked.append(args)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(cleanup.QMessageBox, "question", question)
    cleanup.CleanupMixin._ask_sandbox_trust(win, win.emitted[0])
    assert asked and asked[0][-1] == QMessageBox.StandardButton.No, "No is the default"
    assert str(copy) in asked[0][2]
    assert gamesandbox.untrusted_runtime_copies() == []
    assert win._sandbox_ready_answer is None and win.resynced == 1


def test_startup_no_changes_nothing(tmp_path, rig, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from nadare.gui.main_window_parts import cleanup

    copy = _updated(tmp_path, rig)
    before = gamesandbox.pins_path().read_text()
    monkeypatch.setattr(
        cleanup.QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
    )
    cleanup.CleanupMixin._ask_sandbox_trust(_Window(_profiles()), [str(copy)])
    assert gamesandbox.pins_path().read_text() == before


def test_startup_does_not_ask_when_no_game_wants_the_sandbox(tmp_path, rig, monkeypatch):
    from nadare.gui.main_window_parts import cleanup

    _updated(tmp_path, rig)
    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", False)
    win = _Window(_profiles(on=False))
    cleanup.CleanupMixin._run_sandbox_trust_check(win, _profiles(on=False))
    assert win.emitted == []


def test_startup_says_so_when_a_game_started_meanwhile(tmp_path, rig, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from nadare.gui.main_window_parts import cleanup

    copy = _updated(tmp_path, rig)
    before = gamesandbox.pins_path().read_text()
    monkeypatch.setattr(gamesandbox, "runtime_in_use", lambda copies=None: True)
    monkeypatch.setattr(
        cleanup.QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    told = []
    monkeypatch.setattr(cleanup.QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    cleanup.CleanupMixin._ask_sandbox_trust(_Window(_profiles()), [str(copy)])
    assert gamesandbox.pins_path().read_text() == before
    assert told and "running" in told[0]


def test_the_page_tooltip_mentions_the_update_case():
    from nadare.core.gamefacts import SandboxFacts

    tip = SandboxFacts(supported=True, chosen=True, ready=False).tooltip("default")
    assert "updated" in tip and "start-up" in tip


def test_a_damaged_record_is_not_offered_for_trust(tmp_path, rig):
    """A damaged record trusts nothing and keeps its own hint (delete it and start again)."""
    rig.append(str(_copy(tmp_path)))
    gamesandbox.pins_path().parent.mkdir(parents=True, exist_ok=True)
    gamesandbox.pins_path().write_text("not json")
    assert gamesandbox.untrusted_runtime_copies() == []
