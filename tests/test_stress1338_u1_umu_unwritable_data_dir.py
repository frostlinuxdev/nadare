# SPDX-License-Identifier: GPL-3.0-or-later
"""A data dir that cannot hold the managed umu must become UNAVAILABLE_HINT, not a crash."""

from __future__ import annotations

from pathlib import Path

from nadare.core import umu


def test_resolve_returns_none_when_the_managed_parent_is_not_a_directory(
    tmp_path, monkeypatch
) -> None:
    blocker = tmp_path / "data"
    blocker.write_text("this is a file, not a directory")
    monkeypatch.setattr(umu, "managed_path", lambda: blocker / "umu" / "1.4.4" / "umu-run")
    monkeypatch.setattr(umu, "system", lambda *_a, **_k: None)
    monkeypatch.setattr(umu, "BUNDLED_ARCHIVE", tmp_path / "no-bundled-umu.tar")
    # The interpreter check is not the subject here.
    monkeypatch.setattr(
        umu.os.path,
        "isfile",
        lambda p: True if str(p) == umu.SHEBANG_PYTHON else Path(p).is_file(),
    )
    monkeypatch.setattr(
        umu.shutil, "which", lambda name, *_a, **_k: "/usr/bin/python3"
    )
    # resolve() promises "any failure is logged and reported as None" so the launch can
    # show umu.UNAVAILABLE_HINT. mkdir() currently escapes as NotADirectoryError.
    assert umu.resolve(allow_download=True) is None
