# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the NIKKE launcherless helpers and their pinned Windows binaries.

The two helpers are the only Nadare binaries that carry the game's session, so
their digests are pinned here and the C sources must match the pins too. The
game and launcher layout below mirrors a real install: launcher in
``C:\\NIKKE\\Launcher``, game in ``<root>\\NIKKE\\game``, resources in
``<root>\\Unity\\com_proximabeta_NIKKE``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from nadare.backends.nikke import hosts as h


def write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_prefix(tmp_path: Path, *, root: str = "drive_c/NIKKE") -> tuple[Path, Path]:
    """A prefix with a launcher, service, game and resources; returns (prefix, launcher)."""
    prefix = tmp_path / "prefix"
    base = prefix / root
    launcher = write(base / "Launcher" / "nikke_launcher.exe", b"MZ")
    write(base / "Launcher" / "intl_service" / "intl_service.exe", b"MZ")
    write(base / "NIKKE" / "game" / "nikke.exe", b"MZ")
    (base / "Unity" / "com_proximabeta_NIKKE").mkdir(parents=True)
    return prefix, launcher


def test_helpers_are_the_pinned_builds():
    assert hashlib.sha256(h.INTL_HOST.read_bytes()).hexdigest() == h.INTL_HOST_SHA256
    assert hashlib.sha256(h.SAIL_HOST.read_bytes()).hexdigest() == h.SAIL_HOST_SHA256


def test_sources_match_the_prebuilt_pins():
    here = Path(h.__file__).resolve().parent / "hosts_src"
    assert (
        hashlib.sha256((here / "intlsvc_host.c").read_bytes()).hexdigest()
        == h.INTL_SOURCE_SHA256
    )
    assert (
        hashlib.sha256((here / "sail_host.c").read_bytes()).hexdigest()
        == h.SAIL_SOURCE_SHA256
    )


def test_stage_hosts_copies_both_helpers_privately(tmp_path):
    prefix, _launcher = make_prefix(tmp_path)
    folder = h.stage_hosts(prefix)

    assert folder == prefix / "drive_c" / "Games" / ".nadare" / "nikke"
    assert (folder / h.INTL_HOST_NAME).read_bytes() == h.INTL_HOST.read_bytes()
    assert (folder / h.SAIL_HOST_NAME).read_bytes() == h.SAIL_HOST.read_bytes()
    assert folder.stat().st_mode & 0o777 == 0o700


def test_stage_hosts_refuses_a_tampered_helper(tmp_path, monkeypatch):
    prefix, _launcher = make_prefix(tmp_path)
    tampered = tmp_path / "tampered.exe"
    tampered.write_bytes(b"not the pinned build")
    monkeypatch.setattr(h, "INTL_HOST", tampered)

    with pytest.raises(h.HostError, match="does not match its pin"):
        h.stage_hosts(prefix)


def test_find_game_reads_the_installed_layout(tmp_path):
    prefix, launcher = make_prefix(tmp_path)
    files = h.find_game(launcher, prefix)

    assert files is not None
    assert files.launcher_dir == launcher.parent
    assert files.service == launcher.parent / "intl_service" / "intl_service.exe"
    assert files.game_exe == prefix / "drive_c/NIKKE/NIKKE/game/nikke.exe"
    assert files.game_cwd == files.game_exe.parent
    assert files.resource_dir == prefix / "drive_c/NIKKE/Unity/com_proximabeta_NIKKE"


def test_find_game_without_the_client_is_none(tmp_path):
    prefix, launcher = make_prefix(tmp_path)
    (prefix / "drive_c/NIKKE/NIKKE/game/nikke.exe").unlink()

    assert h.find_game(launcher, prefix) is None


def test_find_game_finds_a_client_only_install_without_resources(tmp_path):
    """The game creates Unity/com_proximabeta_NIKKE on its first run, so its absence is fine."""
    prefix, launcher = make_prefix(tmp_path)
    resources = prefix / "drive_c/NIKKE/Unity/com_proximabeta_NIKKE"
    resources.rmdir()

    files = h.find_game(launcher, prefix)

    assert files is not None
    assert files.game_exe == prefix / "drive_c/NIKKE/NIKKE/game/nikke.exe"
    assert files.resource_dir == resources
    assert not files.resource_dir.exists()


def test_find_game_without_the_service_is_none(tmp_path):
    prefix, launcher = make_prefix(tmp_path)
    (launcher.parent / "intl_service" / "intl_service.exe").unlink()

    assert h.find_game(launcher, prefix) is None


def test_win_path_uses_c_for_the_prefix_and_dosdevices_for_other_drives(tmp_path):
    prefix, launcher = make_prefix(tmp_path)
    assert h.win_path(prefix, launcher) == r"C:\NIKKE\Launcher\nikke_launcher.exe"

    elsewhere = mkdir_drive(tmp_path, prefix, "s")
    assert h.win_path(prefix, elsewhere / "NIKKE" / "game") == r"S:\NIKKE\game"
    assert h.win_path(prefix, tmp_path / "outside").startswith("Z:\\")


def mkdir_drive(tmp_path: Path, prefix: Path, letter: str) -> Path:
    target = tmp_path / f"drive-{letter}"
    target.mkdir(parents=True, exist_ok=True)
    links = prefix / "dosdevices"
    links.mkdir(parents=True, exist_ok=True)
    (links / f"{letter}:").symlink_to(target)
    return target


def test_cache_roundtrip_is_private(tmp_path):
    src = tmp_path / "src"
    write(src / "intl_auth.txt", b"secret-session")
    write(src / "mmkv" / "mmkv.default", b"device-ids")
    cache = tmp_path / "cache"

    h.save_cache(src, cache)
    assert h.has_cache(cache)
    assert (cache / "intl_auth.txt").stat().st_mode & 0o777 == 0o600
    assert (cache / "mmkv" / "mmkv.default").stat().st_mode & 0o777 == 0o600

    dst = tmp_path / "dst"
    h.restore_cache(cache, dst)
    assert (dst / "intl_auth.txt").read_bytes() == b"secret-session"
    assert (dst / "mmkv" / "mmkv.default").read_bytes() == b"device-ids"


def test_save_cache_requires_auth_file(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(h.HostError, match="intl_auth"):
        h.save_cache(src, tmp_path / "cache")


def test_cache_storage_dir_prefers_the_proton_user(tmp_path):
    prefix = tmp_path / "prefix"
    (prefix / "drive_c/users/steamuser").mkdir(parents=True)
    (prefix / "drive_c/users/other").mkdir()

    assert h.cache_storage_dir(prefix) == (
        prefix / "drive_c/users/steamuser/AppData/Roaming/nikke_launcher/29080/intl_service"
    )


def test_cache_storage_dir_falls_back_to_steamuser(tmp_path):
    assert h.cache_storage_dir(tmp_path) == (
        tmp_path / "drive_c/users/steamuser/AppData/Roaming/nikke_launcher/29080/intl_service"
    )


def test_intl_command_carries_service_out_and_login_mode():
    cmd = h.intl_command(
        h.INTL_HOST_NAME,
        r"C:\NIKKE\Launcher\intl_service\intl_service.exe",
        r"C:\tmp\session.enc.json",
        login_channel=6,
        auto_login=0,
    )
    assert cmd[0] == h.INTL_HOST_NAME
    assert cmd[cmd.index("--service") + 1] == (
        r"C:\NIKKE\Launcher\intl_service\intl_service.exe"
    )
    assert cmd[cmd.index("--out") + 1] == r"C:\tmp\session.enc.json"
    assert cmd[cmd.index("--parent-name") + 1] == "nikke_launcher"
    assert cmd[cmd.index("--game-id") + 1] == "29080"
    assert cmd[cmd.index("--login-channel") + 1] == "6"
    assert cmd[cmd.index("--auto-login") + 1] == "0"


def test_sail_command_uses_session_and_launch_triplet():
    cmd = h.sail_command(
        h.SAIL_HOST_NAME,
        r"C:\tmp\session.json",
        r"C:\NIKKE\NIKKE\game\nikke.exe",
        r"C:\NIKKE\NIKKE\game",
        ["-timestamps"],
    )
    assert cmd[:2] == [h.SAIL_HOST_NAME, r"C:\tmp\session.json"]
    assert cmd[2] == "--launch"
    assert cmd[3:6] == [
        r"C:\NIKKE\NIKKE\game\nikke.exe",
        r"C:\NIKKE\NIKKE\game",
        "-timestamps",
    ]


def test_host_env_only_adds_the_sail_names():
    env = h.host_env("/tmp/prefix", launcher_dir=r"C:\NIKKE\Launcher", log=r"C:\log.txt", base={})
    assert env == {
        "WINEPREFIX": "/tmp/prefix",
        "SAIL_LAUNCHER_DIR": r"C:\NIKKE\Launcher",
        "SAIL_HOST_LOG": r"C:\log.txt",
    }


def test_make_session_file_is_private_and_uses_the_resource_path(tmp_path):
    auth = {
        "openid": "1",
        "token": "t",
        "pf": "p",
        "pf_key": "k",
        "bind_list": "[]",
    }
    target = h.make_session_file(
        auth, tmp_path / "session.json", game_resource_path=r"C:\NIKKE\Unity\com_proximabeta_NIKKE"
    )
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text())["game_resource_path"] == (
        r"C:\NIKKE\Unity\com_proximabeta_NIKKE"
    )


def test_private_workdir_is_limited_to_the_owner(tmp_path):
    work = h.private_workdir(tmp_path)
    try:
        assert work.parent == tmp_path
        assert work.stat().st_mode & 0o777 == 0o700
        assert os.listdir(work) == []
    finally:
        work.rmdir()


# ---- links planted in the prefix (round-1 review 2026-11-11) ---------------------------------
# The game, even in the Sandbox, can write anywhere in its prefix. Nadare itself runs outside the
# Sandbox, so nothing it writes or reads there may follow a link the game left.


def test_stage_hosts_does_not_write_through_a_planted_link(tmp_path):
    prefix, _launcher = make_prefix(tmp_path)
    victim = write(tmp_path / "home" / ".bashrc", b"precious")
    stage = prefix.joinpath(*h.STAGE_PARTS)
    stage.mkdir(parents=True)
    (stage / h.SAIL_HOST_NAME).symlink_to(victim)

    folder = h.stage_hosts(prefix)

    assert victim.read_bytes() == b"precious"
    assert not (folder / h.SAIL_HOST_NAME).is_symlink()
    assert (folder / h.SAIL_HOST_NAME).read_bytes() == h.SAIL_HOST.read_bytes()


def test_stage_hosts_refuses_a_linked_stage_folder(tmp_path):
    prefix, _launcher = make_prefix(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    os.chmod(home, 0o755)
    games = prefix / "drive_c" / "Games"
    games.mkdir(parents=True)
    (games / ".nadare").symlink_to(home)

    with pytest.raises(h.HostError):
        h.stage_hosts(prefix)
    assert home.stat().st_mode & 0o777 == 0o755
    assert not (home / "nikke").exists()


def test_save_cache_does_not_copy_a_linked_file_out_of_home(tmp_path):
    secret = write(tmp_path / "home" / ".ssh" / "id_ed25519", b"PRIVATE KEY")
    src = tmp_path / "prefix-cache"
    write(src / "intl_auth.txt", b"session")
    write(src / "mmkv" / "mmkv.default", b"device")
    (src / "IntlRemoteConfig").symlink_to(secret)
    dst = tmp_path / "nadare-cache"

    h.save_cache(src, dst)

    assert (dst / "intl_auth.txt").read_bytes() == b"session"
    assert not (dst / "IntlRemoteConfig").exists()


def test_save_cache_refuses_a_linked_sign_in(tmp_path):
    secret = write(tmp_path / "home" / "secret.txt", b"secret")
    src = tmp_path / "prefix-cache"
    src.mkdir()
    (src / "intl_auth.txt").symlink_to(secret)
    with pytest.raises(h.HostError):
        h.save_cache(src, tmp_path / "nadare-cache")


def test_restore_cache_does_not_write_through_planted_links(tmp_path):
    prefix, _launcher = make_prefix(tmp_path)
    cache = tmp_path / "nadare-cache"
    write(cache / "intl_auth.txt", b"session")
    write(cache / "mmkv" / "mmkv.default", b"device")
    victim = write(tmp_path / "home" / ".profile", b"precious")
    dest = h.cache_storage_dir(prefix)
    dest.mkdir(parents=True)
    (dest / "intl_auth.txt").symlink_to(victim)

    h.restore_cache(cache, dest, root=prefix)

    assert victim.read_bytes() == b"precious"
    assert (dest / "intl_auth.txt").read_bytes() == b"session"
    assert not (dest / "intl_auth.txt").is_symlink()


def test_restore_cache_refuses_a_linked_folder_below_the_prefix(tmp_path):
    prefix, _launcher = make_prefix(tmp_path)
    cache = tmp_path / "nadare-cache"
    write(cache / "intl_auth.txt", b"session")
    write(cache / "mmkv" / "mmkv.default", b"device")
    elsewhere = tmp_path / "home" / "elsewhere"
    elsewhere.mkdir(parents=True)
    dest = h.cache_storage_dir(prefix)
    dest.parent.mkdir(parents=True)
    dest.symlink_to(elsewhere)

    with pytest.raises(h.HostError):
        h.restore_cache(cache, dest, root=prefix)
    assert list(elsewhere.iterdir()) == []


def test_read_auth_does_not_follow_a_link(tmp_path):
    other = write(tmp_path / "home" / "x.json", json.dumps({"token": "t"}).encode())
    link = tmp_path / "session.enc.json"
    link.symlink_to(other)
    with pytest.raises(h.HostError):
        h.read_auth(link)


def test_save_cache_fails_when_a_required_file_cannot_be_copied(tmp_path):
    secret = write(tmp_path / "home" / "x", b"secret")
    src = tmp_path / "prefix-cache"
    write(src / "mmkv" / "mmkv.default", b"device")
    (src / "intl_auth.txt").symlink_to(secret)
    with pytest.raises(h.HostError):
        h.save_cache(src, tmp_path / "nadare-cache")


def test_an_empty_sign_in_is_not_a_cache(tmp_path):
    cache = tmp_path / "c"
    write(cache / "intl_auth.txt", b"")
    write(cache / "mmkv" / "mmkv.default", b"device")
    assert h.has_cache(cache) is False
