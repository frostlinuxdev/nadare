# SPDX-License-Identifier: GPL-3.0-or-later
"""#1422: after a runner change, the first Play upgrades the prefix (~40 s) with no word.

Proton writes its prefix version to ``<compatdata>/version`` (Nadare's prefix is the compatdata),
and the runner's ``proton`` script carries ``CURRENT_PREFIX_VERSION``. When they differ, Proton
upgrades the prefix before the game starts: Play now says so, once per launch.
"""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends.direct import EndfieldBackend
from nadare.core import runners


def _runner(tmp_path, version="dwproton-11.0-17"):
    root = tmp_path / "runner"
    root.mkdir(exist_ok=True)
    (root / "proton").write_text(
        f'#!/usr/bin/env python3\n# proton\nimport os\nCURRENT_PREFIX_VERSION="{version}"\n'
    )
    return root


def _prefix(tmp_path, version="dwproton-11.0-15"):
    prefix = tmp_path / "prefix"
    prefix.mkdir(exist_ok=True)
    if version is not None:
        (prefix / "version").write_text(version + "\n")
    return prefix


def test_a_different_prefix_version_is_an_upgrade(tmp_path):
    assert runners.prefix_upgrade(_prefix(tmp_path), _runner(tmp_path)) == (
        "dwproton-11.0-15",
        "dwproton-11.0-17",
    )


def test_the_same_version_is_not(tmp_path):
    prefix = _prefix(tmp_path, "dwproton-11.0-17")
    assert runners.prefix_upgrade(prefix, _runner(tmp_path)) is None


def test_a_new_prefix_is_not_an_upgrade(tmp_path):
    assert runners.prefix_upgrade(_prefix(tmp_path, None), _runner(tmp_path)) is None


def test_a_runner_without_a_version_line_is_not_guessed(tmp_path):
    root = tmp_path / "runner"
    root.mkdir()
    (root / "proton").write_text("#!/usr/bin/env python3\nprint('hi')\n")
    assert runners.prefix_upgrade(_prefix(tmp_path), root) is None


def test_upstream_proton_versions_work_too(tmp_path):
    prefix = _prefix(tmp_path, "10.1000-200")
    assert runners.prefix_upgrade(prefix, _runner(tmp_path, "11.1000-300")) == (
        "10.1000-200",
        "11.1000-300",
    )


def test_a_linked_version_file_is_not_read(tmp_path):
    prefix = _prefix(tmp_path, None)
    outside = tmp_path / "outside"
    outside.write_text("dwproton-1.0-1\n")
    (prefix / "version").symlink_to(outside)
    assert runners.prefix_upgrade(prefix, _runner(tmp_path)) is None


def test_play_says_so_once(tmp_path):
    prefix = _prefix(tmp_path)
    backend = EndfieldBackend(
        make_context(tmp_path, "direct-endfield", {"prefix": str(prefix)})
    )
    events = []
    backend.ctx.emit = events.append
    runner = _runner(tmp_path)

    backend._note_prefix_upgrade(runner)
    backend._note_prefix_upgrade(runner)

    steps = [e.message for e in events if e.kind == "step"]
    assert len(steps) == 1
    assert "dwproton-11.0-17" in steps[0]
    assert "once" in steps[0]


def test_prefix_command_checks_for_an_upgrade(tmp_path, monkeypatch):
    prefix = _prefix(tmp_path)
    backend = EndfieldBackend(
        make_context(tmp_path, "direct-endfield", {"prefix": str(prefix)})
    )
    runner = _runner(tmp_path)
    seen = []
    monkeypatch.setattr(backend, "resolve_runner", lambda: (runner, "proton"))
    monkeypatch.setattr(backend, "_note_prefix_upgrade", lambda proton: seen.append(proton))
    from nadare.backends.winegame_parts import launching
    from nadare.core import protoncmd

    monkeypatch.setattr(protoncmd.umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(launching, "_refuse_if_userns_blocked", lambda: None)
    monkeypatch.setattr(protoncmd.umu, "note_first_runtime", lambda emit: None)

    try:
        backend._prefix_command(["game.exe"])
    except Exception:  # noqa: BLE001 - only the check matters here
        pass

    assert seen == [runner]
