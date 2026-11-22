# SPDX-License-Identifier: GPL-3.0-or-later
"""Without /usr/bin/python3 the launch runs the managed umu on Nadare's own Python.

Found on NixOS 26.05 through ``appimage-run`` (#1599). Before #1341 item 3 the launch stopped
with "python3 is needed to run umu"; now it starts ``nadare --umu-run`` instead.
"""

from __future__ import annotations

from test_umu_always import _proton

from nadare.core import protoncmd, umu


def test_missing_shebang_python_starts_nadare_umu_run(tmp_path, monkeypatch) -> None:
    backend, _proton_dir = _proton(tmp_path, monkeypatch)
    monkeypatch.setattr(umu, "managed", lambda: umu.managed_path())
    monkeypatch.setattr(umu, "shebang_python_ok", lambda: False)
    monkeypatch.setattr(umu, "prepare_runtime", lambda *a, **k: True)
    monkeypatch.setattr(protoncmd.pvlocales, "prepare", lambda *a, **k: {})
    monkeypatch.setattr(protoncmd.userns, "blocks_runtime", lambda: False)

    argv, _env, _target = backend.build_launch()
    command = umu.command()
    assert argv[: len(command)] == command
    assert command[-1] == "--umu-run"
