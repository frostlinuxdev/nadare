# SPDX-License-Identifier: GPL-3.0-or-later
"""#1838: launcher-free NIKKE heals its launcher folder like its client.

The launcher folder holds the sign-in service the launcher-free start uses. Before, only its four
key files decided whether it was synced, Repair never touched it, a missing ``nikke_launcher.exe``
hid the folder for good, and a launcher check that failed still ended Update as a plain success.
Now every file the last launcher sync recorded counts, Repair syncs the folder too, the folder is
found without the launcher program, and Update says when the launcher could not be checked.
"""

from __future__ import annotations

import hashlib

import test_1245_launcher_current as base

from nadare.backends import launcher as launcher_module
from nadare.core import directdl

env = base.env
api = base.api

EXTRA = {"lib2.pak": b"launcher library", "res/skin.dat": b"skin"}


def _with_ledger(api):
    """The fake install also records what it installed, as the real one does (#1825)."""
    plain = launcher_module.nikke_source.install_launcher
    api["published"].update(EXTRA)
    api["published"]["nikke_launcher.exe"] = b"MZ new launcher"

    def install_launcher(dest, **kw):
        result = plain(dest, **kw)
        files = tuple(
            directdl.RemoteFile(rel, "", len(data), md5=hashlib.md5(data).hexdigest())
            for rel, data in api["published"].items()
        )
        directdl.prune_dropped_files(directdl.Plan("nikke-launcher", "", files=files), dest)
        return result

    return install_launcher


def _key_entries(api):
    """The manifest's entries for the four key files only, as the real source returns."""

    def launcher_key_entries(info, fetch=None):
        return {
            rel: base.SimpleNamespace(size=len(data), md5=hashlib.md5(data).digest())
            for rel, data in api["published"].items()
            if rel in base.KEYS
        }

    return launcher_key_entries


def _installed(env, api, monkeypatch):
    src = launcher_module.nikke_source
    monkeypatch.setattr(src, "install_launcher", _with_ledger(api))
    monkeypatch.setattr(src, "launcher_key_entries", _key_entries(api))
    backend, launcher = base._nikke(env)
    backend._update_launcher_direct(launcher)  # a finished launcher sync: everything current
    api["installs"].clear()
    events = []
    backend.ctx.emit = events.append
    return backend, launcher, events


def test_update_puts_back_a_missing_launcher_file(env, api, monkeypatch):
    backend, launcher, _ = _installed(env, api, monkeypatch)
    (launcher / "lib2.pak").unlink()
    backend.update()
    assert api["installs"] == [launcher]
    assert (launcher / "lib2.pak").read_bytes() == EXTRA["lib2.pak"]


def test_update_puts_back_a_changed_launcher_file(env, api, monkeypatch):
    backend, launcher, _ = _installed(env, api, monkeypatch)
    (launcher / "res/skin.dat").write_bytes(b"SKIN")  # same size, other bytes
    backend.update()
    assert (launcher / "res/skin.dat").read_bytes() == EXTRA["res/skin.dat"]


def test_play_puts_back_a_missing_launcher_file_without_asking_the_publisher(env, api, monkeypatch):
    backend, launcher, _ = _installed(env, api, monkeypatch)
    (launcher / "lib2.pak").unlink()
    backend.ctx.config.data["check_updates"] = False
    backend._update_before_play_locked()
    assert api["installs"] == [launcher]
    assert (launcher / "lib2.pak").exists()


def test_a_current_launcher_with_a_ledger_is_left_alone(env, api, monkeypatch):
    backend, launcher, _ = _installed(env, api, monkeypatch)
    backend._update_before_play_locked()
    assert api["installs"] == []


def test_repair_syncs_the_launcher_folder_too(env, api, monkeypatch):
    backend, launcher, events = _installed(env, api, monkeypatch)
    (launcher / "lib2.pak").unlink()
    backend.repair()
    assert launcher in api["installs"]
    assert (launcher / "lib2.pak").read_bytes() == EXTRA["lib2.pak"]


def test_a_missing_launcher_program_is_put_back_not_forgotten(env, api, monkeypatch):
    backend, launcher, _ = _installed(env, api, monkeypatch)
    (launcher / "nikke_launcher.exe").unlink()
    assert backend._launcher_dir() == launcher
    backend.check_update()
    status = base.SimpleNamespace(data={})
    backend._client_update_status(status)
    assert status.data["update_available"] is True
    backend.update()
    assert (launcher / "nikke_launcher.exe").read_bytes() == b"MZ new launcher"


def test_a_failed_launcher_check_ends_update_with_a_warning(env, api, monkeypatch):
    backend, launcher, events = _installed(env, api, monkeypatch)

    def offline(post=None):
        raise launcher_module.railrepo.RailError("manifest 404")

    monkeypatch.setattr(launcher_module.nikke_source, "launcher_info", offline)
    backend.update()
    steps = [e.message for e in events if e.kind == "step"]
    assert any("launcher" in m and "could not" in m and "manifest 404" in m for m in steps)


def test_without_a_ledger_the_key_files_decide_as_before(env, api):
    backend, launcher = base._nikke(env, files=base.NEW)
    (launcher / "other.pak").write_bytes(b"not recorded anywhere")
    backend._update_before_play_locked()
    assert api["installs"] == []
