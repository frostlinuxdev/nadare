# SPDX-License-Identifier: GPL-3.0-or-later
"""A HoYo update removes what the old version had and the new one does not (like #967).

HoYo Sophon packages are named after the version (``zzz-3.2.0``, ``zzz-3.2.0-audio-en-us``), so
each update wrote a new ledger and never read the old one: nothing was pruned and the old ledger
kept owning the old files forever. Measured on the live CDN (2026-11-08): ZZZ 3.2.0 -> 3.3.0
leaves 710 files, 3.57 GiB of ``.blk`` blocks behind. ``SophonPackage.supersedes`` names the
packages a package replaces.
"""

from __future__ import annotations

from test_sophon import Build, servers  # noqa: F401 - fixture reuse

from nadare.core import sophon
from nadare.core.sources import hoyo

GAME_OLD = r"zzz-[^-/]+"


def _pkg(build, name, supersedes=""):
    base = build.package(name)
    return sophon.SophonPackage(
        name=base.name, manifest_url=base.manifest_url, chunk_url=base.chunk_url,
        manifest_id=base.manifest_id, size=base.size, supersedes=supersedes,
    )


def _state(root):
    return sorted(p.name for p in (root / sophon.STATE_DIR).iterdir())


def test_an_update_prunes_blocks_the_new_version_dropped(servers, tmp_path):  # noqa: F811
    old = Build({"Blocks/1013558155.blk": b"old block" * 20, "game.exe": b"v1"})
    new = Build({"Blocks/2000000001.blk": b"new block" * 20, "game.exe": b"v2"})
    servers(old)
    servers(new)
    sophon.sync_package(_pkg(old, "zzz-3.2.0", GAME_OLD), tmp_path)
    sophon.sync_package(_pkg(new, "zzz-3.3.0", GAME_OLD), tmp_path)
    assert (tmp_path / "game.exe").read_bytes() == b"v2"
    assert (tmp_path / "Blocks" / "2000000001.blk").is_file()
    assert not (tmp_path / "Blocks" / "1013558155.blk").exists()
    assert _state(tmp_path) == ["sophon-zzz-3.3.0.done", "sophon-zzz-3.3.0.files"]


def test_the_audio_package_is_never_taken_for_an_old_game_package(servers, tmp_path):  # noqa: F811
    audio = Build({"Audio/en/a.pck": b"voice" * 10})
    old = Build({"game.exe": b"v1"})
    new = Build({"game.exe": b"v2"})
    for b in (audio, old, new):
        servers(b)
    sophon.sync_package(_pkg(audio, "zzz-3.2.0-audio-en-us", r"zzz-[^-/]+-audio-en-us"), tmp_path)
    sophon.sync_package(_pkg(old, "zzz-3.2.0", GAME_OLD), tmp_path)
    sophon.sync_package(_pkg(new, "zzz-3.3.0", GAME_OLD), tmp_path)
    assert (tmp_path / "Audio" / "en" / "a.pck").is_file()
    assert "sophon-zzz-3.2.0-audio-en-us.files" in _state(tmp_path)


def test_another_language_is_not_superseded(servers, tmp_path):  # noqa: F811
    en = Build({"Audio/en/a.pck": b"en" * 10})
    ja = Build({"Audio/ja/a.pck": b"ja" * 10})
    servers(en)
    servers(ja)
    sophon.sync_package(_pkg(en, "zzz-3.2.0-audio-en-us", r"zzz-[^-/]+-audio-en-us"), tmp_path)
    sophon.sync_package(_pkg(ja, "zzz-3.3.0-audio-ja-jp", r"zzz-[^-/]+-audio-ja-jp"), tmp_path)
    assert (tmp_path / "Audio" / "en" / "a.pck").is_file()


def test_a_file_both_versions_have_is_kept(servers, tmp_path):  # noqa: F811
    old = Build({"shared.bin": b"same" * 30, "old.bin": b"x" * 10})
    new = Build({"shared.bin": b"same" * 30})
    servers(old)
    servers(new)
    sophon.sync_package(_pkg(old, "zzz-3.2.0", GAME_OLD), tmp_path)
    sophon.sync_package(_pkg(new, "zzz-3.3.0", GAME_OLD), tmp_path)
    assert (tmp_path / "shared.bin").read_bytes() == b"same" * 30
    assert not (tmp_path / "old.bin").exists()


def test_without_supersedes_nothing_changes(servers, tmp_path):  # noqa: F811
    old = Build({"old.bin": b"x" * 10})
    new = Build({"new.bin": b"y" * 10})
    servers(old)
    servers(new)
    sophon.sync_package(_pkg(old, "a-1"), tmp_path)
    sophon.sync_package(_pkg(new, "a-2"), tmp_path)
    assert (tmp_path / "old.bin").is_file()
    assert "sophon-a-1.files" in _state(tmp_path)


def test_a_failed_update_keeps_the_old_files_and_ledger(servers, tmp_path, monkeypatch):  # noqa: F811
    old = Build({"old.bin": b"x" * 10})
    new = Build({"new.bin": b"y" * 300})
    servers(old)
    servers(new)
    sophon.sync_package(_pkg(old, "zzz-3.2.0", GAME_OLD), tmp_path)

    def broken(*args, **kwargs):
        raise sophon.SophonError("CDN down")

    monkeypatch.setattr(sophon, "_fetch_chunk", broken)
    try:
        sophon.sync_package(_pkg(new, "zzz-3.3.0", GAME_OLD), tmp_path)
    except sophon.SophonError:
        pass
    assert (tmp_path / "old.bin").is_file()
    assert "sophon-zzz-3.2.0.files" in _state(tmp_path)


def test_hoyo_packages_declare_what_they_supersede():
    game = hoyo._supersedes("zzz", None)
    audio = hoyo._supersedes("zzz", "en-us")
    import re

    assert re.fullmatch(game, "zzz-3.2.0") and not re.fullmatch(game, "zzz-3.2.0-audio-en-us")
    assert re.fullmatch(audio, "zzz-3.2.0-audio-en-us")
    assert not re.fullmatch(audio, "zzz-3.2.0-audio-ja-jp") and not re.fullmatch(audio, "zzz-3.2.0")
    assert not re.fullmatch(game, "genshin-7.1.0")
