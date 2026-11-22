# SPDX-License-Identifier: GPL-3.0-or-later
"""doctor when ``/usr/bin/python3`` is missing: the managed umu still runs (#1341 item 3).

Found on NixOS 26.05 (2026-11-18, #1599): the AppImage started through ``appimage-run``,
whose environment has no ``/usr/bin/python3``. The launch now runs the managed zipapp with
Nadare's own Python (hidden ``--umu-run``), so doctor must not call umu unusable.
"""

from __future__ import annotations

from pathlib import Path

from nadare import app
from nadare.core import graphics, hostlibs, process, runners, umu


def _doctor(
    monkeypatch,
    capsys,
    *,
    python: bool,
    system: str | None,
    managed: str | None = None,
    archive: bool = True,
    image=None,
) -> str:
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(True, "1.4"))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(hostlibs, "image_based", lambda *a, **k: image)
    monkeypatch.setattr(hostlibs, "missing_32bit", lambda *a, **k: [])
    monkeypatch.setattr(umu, "managed", lambda: Path(managed) if managed else None)
    monkeypatch.setattr(
        umu, "_bundled_archive", lambda: umu.managed_path() if archive else None
    )
    monkeypatch.setattr(umu, "shebang_python_ok", lambda: python)
    monkeypatch.setattr(umu, "system", lambda *a, **k: system)
    monkeypatch.setattr(process, "which", lambda name, **k: system)
    monkeypatch.setattr(runners, "all_runners", list)
    app.run_cli(["doctor"])
    return capsys.readouterr().out


def test_with_python3_the_bundled_copy_is_promised_as_before(monkeypatch, capsys) -> None:
    out = _doctor(monkeypatch, capsys, python=True, system=None)
    assert "set up at the first launch from the bundled" in out
    assert "Nadare's own Python" not in out


def test_without_python3_the_bundled_copy_runs_on_nadares_own_python(
    monkeypatch, capsys
) -> None:
    out = _doctor(monkeypatch, capsys, python=False, system=None)
    assert "runs with Nadare's own Python" in out
    assert "verdict: ready" in out
    assert "cannot run" not in out


def test_without_python3_an_installed_copy_runs_on_nadares_own_python(
    monkeypatch, capsys
) -> None:
    out = _doctor(
        monkeypatch, capsys, python=False, system=None, managed="/data/umu/1.4.4/umu-run"
    )
    line = [x for x in out.splitlines() if x.startswith("umu-run:")][0]
    assert "/data/umu/1.4.4/umu-run" in line
    assert "runs with Nadare's own Python" in line
    assert "verdict: ready" in out


def test_without_python3_a_system_umu_is_used_when_no_copy_is_bundled(
    monkeypatch, capsys
) -> None:
    out = _doctor(
        monkeypatch, capsys, python=False, system="/usr/bin/umu-run", archive=False
    )
    line = [x for x in out.splitlines() if x.startswith("umu-run:")][0]
    assert "/usr/bin/umu-run" in line
    assert "/usr/bin/python3" not in line
    assert "verdict: ready" in out


def test_shebang_python_ok_follows_the_shebang_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(umu, "SHEBANG_PYTHON", str(tmp_path / "python3"))
    assert umu.shebang_python_ok() is False
    exe = tmp_path / "python3"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert umu.shebang_python_ok() is True
