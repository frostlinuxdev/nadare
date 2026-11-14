# SPDX-License-Identifier: GPL-3.0-or-later
"""W8-4: a pre-suffix GFL2 record is current for a +client- plan, so status must not update."""

from __future__ import annotations

from _backend_helpers import make_context, write_exe

from nadare.backends.direct import Gfl2Backend
from nadare.core import directdl as dd

OLD = "2.7.4330.14886.28279"
NEW = OLD + "+client-2.7.4330"


def test_client_suffix_alone_is_not_an_update(tmp_path):
    install = tmp_path / "GF2Exilium"
    write_exe(install / "GF2_Exilium.exe", "MZ")
    backend = Gfl2Backend(
        make_context(
            tmp_path,
            "direct-gfl2",
            {"prefix": str(tmp_path / "pfx"), "install_dir": str(install)},
        )
    )
    backend._write_version_file(OLD, "gfl2")
    backend._write_latest(NEW)

    print(
        "recorded:", OLD,
        "| published:", NEW,
        "| directdl._same_version:", dd._same_version(OLD, NEW),
    )
    status = backend.status()
    print("status version:", status.version, "| data:", status.data)

    assert dd._same_version(OLD, NEW) is True, "fixture: directdl should normalise +client-"
    assert status.data["update_available"] is False, (
        "Status compares raw strings, so a client-only suffix is offered as an update"
    )
