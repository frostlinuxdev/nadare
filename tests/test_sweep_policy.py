# SPDX-License-Identifier: GPL-3.0-or-later
"""Policy fixes: installer tree integrity (#42) and binder wording (#112)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nadare.backends.fgo import installer


def _fake_extraction(tmp_path: Path) -> Path:
    dest = tmp_path / "lepton-fgo"
    (dest / "lepton").mkdir(parents=True)
    (dest / "install.sh").write_text("#!/bin/sh\necho install\n", encoding="utf-8")
    (dest / "lepton" / "check-host.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    (dest / "lepton" / "setup-host-selinux.sh").write_text(
        "#!/bin/sh\necho selinux\n", encoding="utf-8"
    )
    (dest / "RELEASE").write_text("lepton-fgo 9.9.9-test\n", encoding="utf-8")
    return dest


def _pin() -> installer.InstallerPin:
    return installer.InstallerPin("9.9.9-test", "lepton-fgo-9.9.9-test.tar.zst", "0" * 64)


def test_stamp_covers_every_file_in_the_extraction(tmp_path: Path) -> None:
    dest = _fake_extraction(tmp_path)
    installer._write_stamp(dest, _pin())
    assert installer._stamp_matches(dest, _pin()) is True


def test_tampered_check_host_invalidates_stamp(tmp_path: Path) -> None:
    dest = _fake_extraction(tmp_path)
    installer._write_stamp(dest, _pin())
    (dest / "lepton" / "check-host.sh").write_text("#!/bin/sh\necho hostile\n", encoding="utf-8")
    assert installer._stamp_matches(dest, _pin()) is False


def test_tampered_setup_host_selinux_invalidates_stamp(tmp_path: Path) -> None:
    dest = _fake_extraction(tmp_path)
    installer._write_stamp(dest, _pin())
    (dest / "lepton" / "setup-host-selinux.sh").write_text(
        "#!/bin/sh\necho hostile\n", encoding="utf-8"
    )
    assert installer._stamp_matches(dest, _pin()) is False


def test_missing_file_invalidates_stamp(tmp_path: Path) -> None:
    dest = _fake_extraction(tmp_path)
    installer._write_stamp(dest, _pin())
    (dest / "lepton" / "check-host.sh").unlink()
    assert installer._stamp_matches(dest, _pin()) is False


def test_old_install_sh_only_stamp_is_a_mismatch(tmp_path: Path) -> None:
    dest = _fake_extraction(tmp_path)
    legacy = {
        "tarball_sha256": _pin().sha256,
        "install_sh_sha256": hashlib.sha256((dest / "install.sh").read_bytes()).hexdigest(),
    }
    (dest / installer._STAMP).write_text(json.dumps(legacy), encoding="utf-8")
    assert installer._stamp_matches(dest, _pin()) is False
