# SPDX-License-Identifier: GPL-3.0-or-later
"""Characterization tests for ``WineGameBackend.status()`` and ``check_update()``.

These pin the *current* behaviour of the shared Wine/Proton status code for the
direct-download and official-launcher games before it is refactored.  They are
deliberately literal: oddities in today's output are recorded, not corrected.

Everything is offline: ``scan_pids`` never sees a real process, the runner
helpers are stubbed, and cached "latest" files live under the isolated XDG tree.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from _backend_helpers import make_context, write_exe
from _parts_patch import patch_everywhere

from nadare.backends import winegame as wg
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.direct import BrownDust2Backend
from nadare.backends.launcher import NikkeBackend
from nadare.backends.stellasora import StellaSoraBackend
from nadare.core import runners
from nadare.core.backends import BackendError

#: ``(id, class, direct_mode)``.  Arknights is both modes; Stella Sora is always direct.
BACKENDS = (
    ("bd2", BrownDust2Backend, True),
    ("nikke", NikkeBackend, False),
    ("arknights-official", ArknightsBackend, False),
    ("arknights-direct", ArknightsBackend, True),
    ("stellasora-direct", StellaSoraBackend, True),
)
DIRECT_BACKENDS = tuple(row for row in BACKENDS if row[2])
OFFICIAL_BACKENDS = tuple(row for row in BACKENDS if not row[2])


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No real VC++ install, runner download/resolve or process scan."""
    monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(wg.runners, "installed_pinned", lambda family="dwproton": None)

    def offline(*_a, **_k):
        raise wg.runners.RunnerError("offline (no downloads in tests)")

    monkeypatch.setattr(wg.runners, "ensure_pinned", offline)
    patch_everywhere(monkeypatch, wg, "scan_pids", lambda patterns: [])
    # The host's Vulkan support must never decide a test.
    monkeypatch.setattr(wg.graphics, "vulkan_note", lambda: "")


def _backend(tmp_path: Path, name: str, cls, direct: bool, profile: dict | None = None):
    values = dict(profile or {})
    # Arknights is only in direct mode when it has a manifest source; the file
    # itself is not read by status(), so a path is enough.
    if direct and cls is ArknightsBackend:
        values.setdefault("manifest_file", str(tmp_path / "manifest.json"))
    return cls(make_context(tmp_path, cls.kind, values, name))


def _install(backend) -> Path:
    """Put the executable status() looks for on disk and return its path."""
    if backend.direct_mode():
        return write_exe(backend.direct_target(), "MZ")
    return write_exe(backend.install_dir() / backend.launcher_names[0], "MZ")


def _write_version(backend, version: str) -> None:
    backend._write_version_file(version, backend.game_tag)


def _write_latest(backend, version: str, checked_at: int | str | None = None) -> None:
    backend._write_latest(version)
    if checked_at is not None:
        path = backend._latest_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["checked_at"] = checked_at
        path.write_text(json.dumps(payload), encoding="utf-8")


def _expected_data(backend, *, direct: bool, exe: str, pids=(), version=None, installed=True):
    data = {
        "prefix": str(backend.prefix()),
        "exe": exe,
        "runner": backend.runner_id(),
        "pids": list(pids),
        "mode": "direct" if direct else "official",
        "can_repair": direct and backend.install_dir().is_dir(),
        "renderer": "",
    }
    if direct:
        if version is not None:
            data["version"] = version
        data["update_available"] = False
    if isinstance(backend, NikkeBackend):
        # NIKKE has a Log in step (the INTL service's own Google sign-in); no saved session here.
        data["logged_in"] = False
        # Its Log in runs the game's own INTL service, so it waits for the install.
        data["can_login"] = installed
        # Its Log in is the Google sign-in only, and the button says so (2026-11-11).
        data["login_label"] = "Log in with Google"
    return data


class TestStatusNotInstalled:
    @pytest.mark.parametrize(("name", "cls", "direct"), BACKENDS)
    def test_not_installed(self, tmp_path, name, cls, direct):
        backend = _backend(tmp_path, name, cls, direct)
        status = backend.status()
        target = backend.direct_target() if direct else backend.find_launcher()

        assert status.state == "not_installed"
        assert status.running is False
        assert status.version is None
        assert status.message == f"{backend.display_name} is not installed yet"
        # A direct backend still reports its *intended* path even though no file
        # exists; official mode reports the failed launcher search as "".
        assert status.data["exe"] == (str(target) if direct else "")
        assert status.data == _expected_data(
            backend, direct=direct, exe=status.data["exe"], installed=False
        )

    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    def test_direct_exe_with_incomplete_marker(self, tmp_path, name, cls, direct):
        backend = _backend(tmp_path, name, cls, direct)
        exe = _install(backend)
        backend._install_marker_path().write_text("sync in progress\n", encoding="utf-8")

        status = backend.status()

        assert status.state == "not_installed"
        assert status.running is False
        assert status.message.startswith("installation incomplete — ")
        assert "click RESUME to finish" in status.message  # and the size on disk (#991)
        assert status.data["exe"] == str(exe)
        assert status.data["can_repair"] is True
        assert status.data["incomplete"] is True
        assert status.data == {
            **_expected_data(backend, direct=True, exe=str(exe)),
            "incomplete": True,
            "partial_bytes": status.data["partial_bytes"],
        }


class TestStatusInstalled:
    @pytest.mark.parametrize(("name", "cls", "direct"), BACKENDS)
    def test_installed_without_version_or_cache(self, tmp_path, name, cls, direct):
        backend = _backend(tmp_path, name, cls, direct)
        exe = _install(backend)

        status = backend.status()

        assert status.state == "installed"
        assert status.running is False
        assert status.version is None
        assert status.message == ""
        assert status.data["exe"] == str(exe)
        expected = _expected_data(backend, direct=direct, exe=str(exe))
        if direct:
            # No version file: the key is simply absent from data.
            assert "version" not in status.data
            assert status.data["update_available"] is False
        assert status.data == expected

    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    def test_version_file_without_cached_latest(self, tmp_path, name, cls, direct):
        backend = _backend(tmp_path, name, cls, direct)
        exe = _install(backend)
        _write_version(backend, "1.0.0")

        status = backend.status()

        assert status.state == "installed"
        assert status.version == "1.0.0"
        assert status.data["version"] == "1.0.0"
        assert status.data["update_available"] is False
        assert status.data == _expected_data(
            backend, direct=True, exe=str(exe), version="1.0.0"
        )

    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    @pytest.mark.parametrize(
        ("recorded", "latest", "available"),
        [
            ("1.0.0", "1.1.0", True),
            ("1.0.0", "1.0.0", False),
            ("1.0.0", "0.9.0", True),
        ],
    )
    def test_cached_latest_drives_update_available(
        self, tmp_path, name, cls, direct, recorded, latest, available
    ):
        backend = _backend(tmp_path, name, cls, direct)
        exe = _install(backend)
        _write_version(backend, recorded)
        _write_latest(backend, latest)

        status = backend.status()

        assert status.state == "installed"
        assert status.version == recorded
        assert status.data["update_available"] is available
        expected = _expected_data(backend, direct=True, exe=str(exe), version=recorded)
        expected["update_available"] = available
        if available:
            expected["latest_version"] = latest  # named on the game page next to the version
        assert status.data == expected

    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    def test_missing_version_file_ignores_cached_latest(self, tmp_path, name, cls, direct):
        backend = _backend(tmp_path, name, cls, direct)
        _install(backend)
        _write_latest(backend, "9.9.9")

        status = backend.status()

        assert status.state == "installed"
        assert status.version is None
        assert "version" not in status.data
        assert status.data["update_available"] is False

    @pytest.mark.parametrize(("name", "cls", "direct"), BACKENDS)
    def test_running_when_pids_are_found(self, tmp_path, name, cls, direct, monkeypatch):
        backend = _backend(tmp_path, name, cls, direct)
        exe = _install(backend)
        patch_everywhere(monkeypatch, wg, "scan_pids", lambda patterns: [4242])

        status = backend.status()

        assert status.state == "running"
        assert status.running is True
        assert status.data["pids"] == [4242]
        assert status.data["exe"] == str(exe)
        assert status.data == _expected_data(
            backend, direct=direct, exe=str(exe), pids=[4242]
        )

    @pytest.mark.parametrize(("name", "cls", "direct"), BACKENDS)
    def test_vulkan_note_becomes_the_installed_message(
        self, tmp_path, name, cls, direct, monkeypatch
    ):
        backend = _backend(tmp_path, name, cls, direct)
        _install(backend)
        note = "Your graphics driver looks old for Vulkan games."
        monkeypatch.setattr(wg.graphics, "vulkan_note", lambda: note)

        status = backend.status()

        assert status.state == "installed"
        assert status.message == note

    @pytest.mark.parametrize(("name", "cls", "direct"), BACKENDS)
    def test_renderer_is_reported_from_the_cached_probe(self, tmp_path, name, cls, direct):
        backend = _backend(tmp_path, name, cls, direct)
        _install(backend)
        backend._renderer = "vulkan"

        status = backend.status()

        assert status.data["renderer"] == "vulkan"


class TestStatusMissingRunner:
    @pytest.mark.parametrize(("name", "cls", "direct"), BACKENDS)
    def test_explicit_runner_that_no_longer_resolves_is_an_error(
        self, tmp_path, name, cls, direct, monkeypatch
    ):
        backend = _backend(
            tmp_path, name, cls, direct, {"runner": "MyDeletedProton"}
        )
        exe = _install(backend)

        def missing(_value):
            raise runners.RunnerError("the tested runner is not installed")

        monkeypatch.setattr(wg.runners, "resolve_runner", missing)

        status = backend.status()

        assert status.state == "error"
        assert status.running is False
        assert status.message == (
            "Game files are present but the Proton runner is missing: "
            "choose or install a runner in Settings."
        )
        assert status.data["runner"] == "MyDeletedProton"
        assert status.data["exe"] == str(exe)

    @pytest.mark.parametrize(("name", "cls", "direct"), BACKENDS)
    def test_default_family_runner_never_errors(self, tmp_path, name, cls, direct, monkeypatch):
        backend = _backend(tmp_path, name, cls, direct)
        _install(backend)

        def boom(_value):
            raise AssertionError("the default family must not call resolve_runner")

        monkeypatch.setattr(wg.runners, "resolve_runner", boom)

        status = backend.status()

        assert status.state == "installed"
        assert status.data["runner"] == ""


class TestStatusCanRepair:
    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    def test_true_for_an_existing_install_dir(self, tmp_path, name, cls, direct):
        backend = _backend(tmp_path, name, cls, direct)
        _install(backend)
        assert backend.install_dir().is_dir()
        assert backend.status().data["can_repair"] is True

    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    def test_false_without_a_install_dir(self, tmp_path, name, cls, direct):
        backend = _backend(tmp_path, name, cls, direct)
        assert not backend.install_dir().is_dir()
        assert backend.status().data["can_repair"] is False

    @pytest.mark.parametrize(("name", "cls", "direct"), OFFICIAL_BACKENDS)
    def test_false_in_official_mode_even_with_an_install_dir(self, tmp_path, name, cls, direct):
        backend = _backend(tmp_path, name, cls, direct)
        _install(backend)
        assert backend.install_dir().is_dir()
        assert backend.status().data["can_repair"] is False


class TestCheckUpdate:
    @pytest.mark.parametrize(("name", "cls", "direct"), OFFICIAL_BACKENDS)
    def test_official_mode_returns_immediately(self, tmp_path, name, cls, direct, monkeypatch):
        backend = _backend(tmp_path, name, cls, direct)
        monkeypatch.setattr(backend, "_load_manifest", lambda: pytest.fail("fetched"))

        backend.check_update()

        assert backend._read_latest() is None

    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    def test_respects_the_check_updates_setting(self, tmp_path, name, cls, direct, monkeypatch):
        backend = _backend(tmp_path, name, cls, direct)
        backend.ctx.config.data["check_updates"] = False
        monkeypatch.setattr(backend, "_load_manifest", lambda: pytest.fail("fetched"))

        backend.check_update()

        assert backend._read_latest() is None

    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    def test_fresh_cache_is_not_fetched_again(self, tmp_path, name, cls, direct, monkeypatch):
        backend = _backend(tmp_path, name, cls, direct)
        _write_latest(backend, "1.0.0", checked_at=int(time.time()))
        before = backend._read_latest()
        monkeypatch.setattr(backend, "_load_manifest", lambda: pytest.fail("fetched"))

        backend.check_update()

        assert backend._read_latest() == before

    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    @pytest.mark.parametrize("with_stale_cache", [False, True])
    def test_none_or_stale_cache_loads_and_writes_the_manifest_version(
        self, tmp_path, name, cls, direct, with_stale_cache
    ):
        backend = _backend(tmp_path, name, cls, direct)
        if with_stale_cache:
            _write_latest(backend, "0.1.0", checked_at=1)
        manifest = SimpleNamespace(version="9.9.9", game_tag=backend.game_tag)
        backend._load_manifest = lambda: manifest

        backend.check_update()

        latest = backend._read_latest()
        assert latest is not None
        assert latest["version"] == "9.9.9"
        assert latest["checked_at"] > 1

    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    @pytest.mark.parametrize(
        "error", [BackendError("bad manifest"), RuntimeError("boom"), OSError("disk")]
    )
    def test_load_manifest_errors_are_swallowed_and_leave_the_cache(
        self, tmp_path, name, cls, direct, error
    ):
        backend = _backend(tmp_path, name, cls, direct)
        _write_latest(backend, "1.0.0", checked_at=1)
        before = backend._read_latest()

        def fail():
            raise error

        backend._load_manifest = fail

        backend.check_update()  # must not raise

        assert backend._read_latest() == before

    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    def test_write_latest_oserror_is_swallowed(self, tmp_path, name, cls, direct):
        backend = _backend(tmp_path, name, cls, direct)
        backend._load_manifest = lambda: SimpleNamespace(version="9.9.9")

        def fail(_version):
            raise OSError("read-only cache")

        backend._write_latest = fail

        backend.check_update()  # must not raise

    @pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
    def test_garbage_checked_at_is_treated_as_stale(self, tmp_path, name, cls, direct):
        backend = _backend(tmp_path, name, cls, direct)
        _write_latest(backend, "0.1.0", checked_at="not-a-timestamp")
        backend._load_manifest = lambda: SimpleNamespace(version="9.9.9")

        backend.check_update()

        latest = backend._read_latest()
        assert latest is not None
        assert latest["version"] == "9.9.9"
        assert isinstance(latest["checked_at"], int)
