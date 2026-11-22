# SPDX-License-Identifier: GPL-3.0-or-later
"""#1612/#1613: say what the NTE install and first start will do.

#1612: after the loose files (89 GB for NTE) the sync installs packs one after another; the steps
said only "installing <name>", so it read as a surprise second download. #1613: the pinned launcher
installer is still the one Perfect World's site serves (checked 2026-11-18), so the launcher updates
itself on the first start; the page said only "the Update button once".
"""

from __future__ import annotations

from test_directdl_prune import make_zip, md5, server  # noqa: F401 - fixture reuse

from nadare.backends.direct import NteDirectBackend
from nadare.core import directdl as dd


def _steps(events):
    return [e.message for e in events if e.kind == "step"]


def _archive(srv, name, blob, path):
    return dd.Archive(name, dd.RemoteFile(f"{name}.zip", srv.url + path, len(blob), md5=md5(blob)))


def test_packs_are_counted_and_the_second_pass_is_announced(server, tmp_path):  # noqa: F811
    z1 = make_zip({"a.pak": b"one"})
    z2 = make_zip({"b.pak": b"two"})
    loose = b"loose content"
    srv = server({"/1.zip": z1, "/2.zip": z2, "/loose.bin": loose})
    plan = dd.Plan(
        "T",
        "1",
        files=(dd.RemoteFile("loose.bin", srv.url + "/loose.bin", len(loose), md5=md5(loose)),),
        archives=(_archive(srv, "p1", z1, "/1.zip"), _archive(srv, "p2", z2, "/2.zip")),
    )
    events: list = []
    dd.sync(plan, tmp_path, emit=events.append)
    steps = _steps(events)
    assert any("pack 1 of 2" in s and "p1" in s for s in steps), steps
    assert any("pack 2 of 2" in s and "p2" in s for s in steps), steps
    second = [s for s in steps if "second pass" in s.lower()]
    assert len(second) == 1, steps


def test_packs_alone_do_not_claim_a_second_pass(server, tmp_path):  # noqa: F811
    z1 = make_zip({"a.pak": b"one"})
    srv = server({"/1.zip": z1})
    events: list = []
    dd.sync(
        dd.Plan("T", "1", archives=(_archive(srv, "p1", z1, "/1.zip"),)),
        tmp_path,
        emit=events.append,
    )
    steps = _steps(events)
    assert any("pack 1 of 1" in s for s in steps), steps
    assert not any("second pass" in s.lower() for s in steps), steps


def test_nte_page_says_two_passes_and_the_launcher_self_update():
    text = NteDirectBackend.page_notice[0]
    assert "two passes" in text
    assert "updates itself" in text
