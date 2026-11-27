# SPDX-License-Identifier: GPL-3.0-or-later
"""#1836: an R1999 update does not leave player-deleted or damaged files missing.

The diff package only carries the files that changed. When a file from an earlier archive is
gone (or has another size), the update takes the full package instead, the way Repair does.
"""

from __future__ import annotations

from test_r1999_lifecycle import _backend, _delta_install, publisher  # noqa: F401

from nadare.core import directdl as dd


def _base_install(publisher, backend):  # noqa: F811
    install = backend.install_dir()
    dd.sync(
        publisher.plan("3.9.0", {"fullPackage": publisher.base, "diffPackage": None}),
        install,
        workers=1,
    )
    backend._write_version_file("3.9.0", "r1999")
    (install / "reverse1999.exe").write_bytes(b"exe")
    return install


def _resource_versions(publisher):  # noqa: F811
    return [body["currentVersion"] for url, body in publisher.calls if url.endswith("/resource")]


def test_a_file_the_diff_does_not_touch_comes_back(publisher, tmp_path):  # noqa: F811
    backend = _backend(tmp_path)
    install = _base_install(publisher, backend)
    (install / "keep.bin").unlink()
    publisher.calls.clear()

    backend.update()

    assert (install / "keep.bin").read_bytes() == b"base keep"
    assert (install / "updated.bin").read_bytes() == b"new"
    assert _resource_versions(publisher) == [""]


def test_a_file_with_another_size_takes_the_full_package(publisher, tmp_path):  # noqa: F811
    backend = _backend(tmp_path)
    install = _base_install(publisher, backend)
    (install / "keep.bin").write_bytes(b"cut")
    publisher.calls.clear()

    backend.update()

    assert (install / "keep.bin").read_bytes() == b"base keep"
    assert _resource_versions(publisher) == [""]


def test_a_complete_install_still_takes_the_diff(publisher, tmp_path):  # noqa: F811
    backend = _backend(tmp_path)
    install = _base_install(publisher, backend)
    (install / "my-notes.txt").write_text("player's own file")
    publisher.calls.clear()

    backend.update()

    assert (install / "updated.bin").read_bytes() == b"new"
    assert _resource_versions(publisher) == ["3.9.0"]


def test_the_newest_receipt_decides_a_replaced_file_size(publisher, tmp_path):  # noqa: F811
    # retired.bin is 4 bytes in the base and 5 after the delta: the delta's size counts.
    backend = _backend(tmp_path)
    _delta_install(publisher, backend)
    assert backend._installed_files_missing() == []


def test_a_link_in_place_of_a_game_file_counts_as_missing(publisher, tmp_path):  # noqa: F811
    backend = _backend(tmp_path)
    install = _base_install(publisher, backend)
    outside = tmp_path / "elsewhere.bin"
    outside.write_bytes(b"base keep")
    (install / "keep.bin").unlink()
    (install / "keep.bin").symlink_to(outside)
    assert backend._installed_files_missing() == ["keep.bin"]
