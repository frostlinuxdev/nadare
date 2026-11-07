# SPDX-License-Identifier: GPL-3.0-or-later
"""#771: the AppImage updates itself: where it lives, what it downloads, and how it is replaced."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest
from _httpserver import Server

from nadare.core import selfupdate
from nadare.core.download import DownloadError, IntegrityError
from nadare.core.updatecheck import UpdateInfo

INFO = UpdateInfo(
    version="0.1.0b20",
    url="https://github.com/frostlinuxdev/nadare/releases/tag/v0.1.0b20",
    name="Nadare 0.1.0b20",
)
NAME = "Nadare-0.1.0b20-x86_64.AppImage"
BODY = b"\x7fELF-fake-appimage-bytes" * 50
DIGEST = hashlib.sha256(BODY).hexdigest()


# ---------------------------------------------------------------------------
# asset URLs and the download host allow-list
# ---------------------------------------------------------------------------


def test_asset_urls_follow_the_release_naming_scheme():
    urls = selfupdate.asset_urls(INFO)
    base = "https://github.com/frostlinuxdev/nadare/releases/download/v0.1.0b20/"
    assert urls.appimage == base + NAME
    assert urls.sums == base + "SHA256SUMS"
    assert urls.filename == NAME


@pytest.mark.parametrize(
    "url",
    [
        "https://forge.example.org/admin/nadare/releases/tag/v0.1.0b20",
        "http://github.com/frostlinuxdev/nadare/releases/tag/v0.1.0b20",
        "https://github.com.evil.example/frostlinuxdev/nadare/releases/tag/v0.1.0b20",
        "https://github.com/frostlinuxdev/nadare/releases",
        "https://github.com/frostlinuxdev/nadare/releases/tag/../../x",
        "https://github.com/frostlinuxdev/nadare/releases/tag/v0.1.0b19",  # not this version
        "https://github.com/../nadare/releases/tag/v0.1.0b20",
        "https://github.com/frostlinuxdev/../releases/tag/v0.1.0b20",
        "",
    ],
)
def test_only_github_release_pages_are_self_updatable(url):
    assert selfupdate.asset_urls(UpdateInfo("0.1.0b20", url, "n")) is None


def test_downloads_may_only_go_to_github_hosts_over_https():
    ok = selfupdate.download_host_allowed
    assert ok("https://github.com/x/y/releases/download/v1/a")
    assert ok("https://objects.githubusercontent.com/abc")
    assert ok("https://release-assets.githubusercontent.com/abc")
    assert not ok("http://github.com/x")
    assert not ok("https://github.com.evil.example/x")
    assert not ok("https://evilgithubusercontent.com/x")
    assert not ok("https://example.org/x")


# ---------------------------------------------------------------------------
# which file would be replaced
# ---------------------------------------------------------------------------


def _appimage(tmp_path: Path, name: str = "Nadare-0.1.0b19-x86_64.AppImage") -> Path:
    path = tmp_path / name
    path.write_bytes(b"old")
    path.chmod(0o755)
    return path


def test_the_running_appimage_is_the_target(tmp_path):
    path = _appimage(tmp_path)
    assert selfupdate.appimage_target({"APPIMAGE": str(path)}) == path


def test_no_target_without_an_appimage(tmp_path):
    assert selfupdate.appimage_target({}) is None
    assert selfupdate.appimage_target({"APPIMAGE": str(tmp_path / "missing.AppImage")}) is None
    plain = tmp_path / "nadare"
    plain.write_bytes(b"x")
    assert selfupdate.appimage_target({"APPIMAGE": str(plain)}) is None


def test_no_target_in_a_folder_that_cannot_be_written(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    path = _appimage(tmp_path)
    tmp_path.chmod(0o555)
    try:
        assert selfupdate.appimage_target({"APPIMAGE": str(path)}) is None
    finally:
        tmp_path.chmod(0o755)


def test_package_managed_builds_never_update_themselves(tmp_path):
    path = _appimage(tmp_path)
    env = {"APPIMAGE": str(path), "NADARE_MANAGED_UPDATES": "1"}
    assert selfupdate.appimage_target(env) is None


# ---------------------------------------------------------------------------
# the expected hash
# ---------------------------------------------------------------------------


def _sums(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _index(version: str = "0.1.0b20", digest: str = DIGEST):
    return lambda: {"schema": 1, "releases": {version: {"x86_64": {"sha256": digest}}}}


def test_the_index_hash_is_used_and_the_release_sums_must_agree():
    sums = _sums(f"{DIGEST}  {NAME}", f"{'0' * 64}  other.AppImage")
    expected = selfupdate.expected_sha256(INFO, fetch_sums=lambda url: sums, fetch_index=_index())
    assert expected.sha256 == DIGEST and expected.source == "index"


def test_the_release_sums_alone_are_never_enough_when_the_index_has_no_entry():
    sums = _sums(f"{DIGEST} *{NAME}")
    with pytest.raises(selfupdate.SelfUpdateError, match="index"):
        selfupdate.expected_sha256(
            INFO, fetch_sums=lambda url: sums, fetch_index=lambda: {"schema": 1, "releases": {}}
        )


def test_a_disagreement_between_index_and_release_sums_refuses_the_update():
    sums = _sums(f"{'1' * 64}  {NAME}")
    with pytest.raises(selfupdate.SelfUpdateError, match="disagree"):
        selfupdate.expected_sha256(INFO, fetch_sums=lambda url: sums, fetch_index=_index())


def test_no_hash_anywhere_refuses_the_update():
    with pytest.raises(selfupdate.SelfUpdateError, match="no checksum"):
        selfupdate.expected_sha256(
            INFO, fetch_sums=lambda url: "", fetch_index=lambda: {"schema": 1, "releases": {}}
        )


def test_a_dead_index_refuses_the_update_even_with_release_sums():
    def broken():
        raise OSError("offline")

    sums = _sums(f"{DIGEST}  {NAME}")
    with pytest.raises(selfupdate.SelfUpdateError, match="index"):
        selfupdate.expected_sha256(INFO, fetch_sums=lambda url: sums, fetch_index=broken)


def test_the_release_sums_are_not_even_fetched_without_an_index_hash():
    fetched = []

    def sums(url):
        fetched.append(url)
        return _sums(f"{DIGEST}  {NAME}")

    with pytest.raises(selfupdate.SelfUpdateError):
        selfupdate.expected_sha256(
            INFO, fetch_sums=sums, fetch_index=lambda: {"schema": 1, "releases": {}}
        )
    assert fetched == []


def test_an_unreadable_release_sums_file_does_not_block_an_indexed_update():
    def broken(url):
        raise OSError("sums unreachable")

    expected = selfupdate.expected_sha256(INFO, fetch_sums=broken, fetch_index=_index())
    assert expected.sha256 == DIGEST and expected.source == "index"


@pytest.mark.parametrize("bad", ["", "zz" * 32, "a" * 63, "A" * 65, "g" * 64])
def test_a_malformed_hash_is_never_accepted(bad):
    with pytest.raises(selfupdate.SelfUpdateError):
        selfupdate.expected_sha256(
            INFO,
            fetch_sums=lambda url: _sums(f"{bad}  {NAME}"),
            fetch_index=lambda: {"schema": 1, "releases": {}},
        )


def test_the_index_entry_for_another_version_is_ignored():
    sums = _sums(f"{DIGEST}  {NAME}")
    with pytest.raises(selfupdate.SelfUpdateError, match="index"):
        selfupdate.expected_sha256(
            INFO,
            fetch_sums=lambda url: sums,
            fetch_index=_index(version="0.1.0b19", digest="1" * 64),
        )


# ---------------------------------------------------------------------------
# download and verify
# ---------------------------------------------------------------------------


def test_a_verified_download_is_executable(tmp_path):
    server = Server({"/a": BODY}, keepalive=True)
    try:
        dest = tmp_path / "new.AppImage"
        selfupdate.download_verified(server.url + "/a", dest, DIGEST, allowed=lambda url: True)
        assert dest.read_bytes() == BODY
        assert dest.stat().st_mode & stat.S_IXUSR
    finally:
        server.close()


def test_a_wrong_hash_leaves_nothing_behind(tmp_path):
    server = Server({"/a": BODY}, keepalive=True)
    try:
        dest = tmp_path / "new.AppImage"
        with pytest.raises(IntegrityError):
            selfupdate.download_verified(
                server.url + "/a", dest, "0" * 64, allowed=lambda url: True
            )
        assert not dest.exists()
        assert not list(tmp_path.glob("*.part"))
    finally:
        server.close()


def test_a_wrong_hash_says_so_in_plain_words(tmp_path):
    """Seen on the VM: the message named the temp file. It should say what happened."""
    server = Server({"/a": BODY}, keepalive=True)
    try:
        dest = tmp_path / ".Nadare-0.1.0b21-x86_64.AppImage.download"
        with pytest.raises(IntegrityError) as caught:
            selfupdate.download_verified(
                server.url + "/a", dest, "0" * 64, allowed=lambda url: True
            )
        message = str(caught.value)
        assert "checksum" in message and "not installed" in message
        assert ".download" not in message and "sha256 mismatch" not in message
    finally:
        server.close()


def test_a_redirect_to_a_foreign_host_is_refused(tmp_path):
    seen: list[str] = []

    def allowed(url: str) -> bool:
        seen.append(url)
        return False

    server = Server({"/a": BODY})
    try:
        # The first URL itself is the one being checked here: it is not allowed.
        with pytest.raises((DownloadError, selfupdate.SelfUpdateError)):
            selfupdate.download_verified(
                server.url + "/a", tmp_path / "x", DIGEST, allowed=allowed
            )
        assert not (tmp_path / "x").exists()
    finally:
        server.close()


# ---------------------------------------------------------------------------
# replacing the file
# ---------------------------------------------------------------------------


def test_install_replaces_the_file_and_keeps_the_old_one(tmp_path):
    target = _appimage(tmp_path)
    new = tmp_path / "download.AppImage"
    new.write_bytes(b"new-bytes")
    new.chmod(0o755)
    result = selfupdate.install_update(new, target, NAME)
    assert result.read_bytes() == b"new-bytes"
    assert result.name == NAME and result.parent == tmp_path
    backup = tmp_path / (target.name + ".previous")
    assert backup.read_bytes() == b"old"
    # the old name now leads to the new file, so a shortcut made with it keeps working (#869)
    assert target.is_symlink() and target.read_bytes() == b"new-bytes"
    assert not new.exists()


def test_install_puts_the_old_file_back_when_the_new_one_cannot_be_moved(tmp_path, monkeypatch):
    target = _appimage(tmp_path)
    new = tmp_path / "download.AppImage"
    new.write_bytes(b"new-bytes")
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        if Path(src).name == "download.AppImage":
            raise OSError("disk went away")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(selfupdate.SelfUpdateError):
        selfupdate.install_update(new, target, NAME)
    assert target.read_bytes() == b"old"  # nothing lost
    assert new.exists()


def test_install_over_an_existing_file_of_the_new_name_still_works(tmp_path):
    target = _appimage(tmp_path)
    existing = tmp_path / NAME
    existing.write_bytes(b"stale")
    new = tmp_path / "download.AppImage"
    new.write_bytes(b"new-bytes")
    result = selfupdate.install_update(new, target, NAME)
    assert result.read_bytes() == b"new-bytes"


def test_the_previous_copy_goes_once_the_new_version_has_started(tmp_path):
    target = _appimage(tmp_path, "Nadare-0.1.0b19-x86_64.AppImage")
    backup = tmp_path / (target.name + ".previous")
    backup.write_bytes(b"old")
    running = tmp_path / NAME
    running.write_bytes(b"new")
    selfupdate.cleanup_previous(running)
    assert not backup.exists()
    assert running.exists()


def test_cleanup_does_not_touch_other_files(tmp_path):
    running = _appimage(tmp_path, NAME)
    other = tmp_path / "notes.txt"
    other.write_text("keep")
    selfupdate.cleanup_previous(running)
    assert other.exists() and running.exists()


# ---------------------------------------------------------------------------
# restarting
# ---------------------------------------------------------------------------


def test_the_restart_command_runs_the_new_file_with_a_clean_appimage_environment(tmp_path):
    new = tmp_path / NAME
    env = {
        "PATH": "/usr/bin",
        "APPIMAGE": "/old/path.AppImage",
        "APPDIR": "/tmp/.mount_old",
        "ARGV0": "old",
        "OWD": "/home/x",
        "HOME": "/home/x",
    }
    argv, clean = selfupdate.restart_command(new, env)
    assert argv[-2:] == [str(new), "gui"] or str(new) in argv
    assert "gui" in argv
    for key in ("APPIMAGE", "APPDIR", "ARGV0", "OWD"):
        assert key not in clean
    assert clean["HOME"] == "/home/x" and clean["PATH"] == "/usr/bin"


def test_the_default_checksum_fetch_reads_a_real_file():
    sums = f"{DIGEST}  {NAME}\n".encode()
    server = Server({"/SHA256SUMS": sums}, keepalive=True)
    try:
        assert selfupdate._fetch_sums(server.url + "/SHA256SUMS") == sums.decode()
    finally:
        server.close()


def test_an_oversized_checksum_file_is_refused():
    server = Server({"/SHA256SUMS": b"x" * (selfupdate._SUMS_MAX_BYTES + 10)}, keepalive=True)
    try:
        with pytest.raises(DownloadError):
            selfupdate._fetch_sums(server.url + "/SHA256SUMS")
    finally:
        server.close()


def test_the_restart_waits_for_the_old_process_to_exit_before_starting(tmp_path):
    """Review wave 3: a fixed sleep can start the new instance while the old one is still
    shutting down, and the new one would then just hand over to it and exit."""
    import subprocess
    import time

    marker = tmp_path / "started"
    new = tmp_path / "new.AppImage"
    new.write_text(f"#!/bin/sh\necho started > {marker}\n")
    new.chmod(0o755)
    old = subprocess.Popen(["sleep", "2"])
    try:
        argv, env = selfupdate.restart_command(new, {"PATH": os.environ["PATH"]}, pid=old.pid)
        starter = subprocess.Popen(argv, env=env, start_new_session=True)
        time.sleep(1.2)
        assert not marker.exists(), "started while the old process was still running"
        old.wait(timeout=5)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        starter.wait(timeout=5)
    finally:
        old.kill()
        old.wait()


def test_the_restart_gives_up_waiting_for_an_old_process_that_never_exits(tmp_path):
    argv, _env = selfupdate.restart_command(tmp_path / NAME, {}, pid=123)
    script = argv[2]
    assert "kill -0" in script and "-lt" in script  # bounded: it must not wait forever
