# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for nadare.core.backends."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from nadare.core import paths
from nadare.core.backends import (
    BackendContext,
    BackendError,
    BackendUnsupported,
    GameBackend,
    Status,
    create,
    get,
    kinds,
    register,
)
from nadare.core.config import Config


class DummyBackend(GameBackend):
    kind = "dummy-test"

    def status(self) -> Status:
        return Status("installed")

    def install(self) -> None:
        pass

    def update(self) -> None:
        pass

    def launch(self) -> int:
        return 0

    def stop(self) -> None:
        pass


def test_register_get_and_kinds() -> None:
    register("dummy-test", DummyBackend)

    assert get("dummy-test") is DummyBackend
    assert "dummy-test" in kinds()
    assert kinds() == sorted(kinds())


def test_get_unknown_backend_raises() -> None:
    with pytest.raises(BackendError) as excinfo:
        get("no-such-backend-xyz")
    assert "unknown backend" in str(excinfo.value)
    assert excinfo.value.code == 1


def test_create_unknown_backend_raises(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("p", backend="no-such-backend-xyz")

    with pytest.raises(BackendError):
        create("p", cfg)


def test_create_profile_without_backend_raises(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("p")

    with pytest.raises(BackendError) as excinfo:
        create("p", cfg)
    assert "no backend set" in str(excinfo.value)


def test_create_unknown_profile_raises_backend_error(tmp_path: Path) -> None:
    """L17: a missing profile must raise BackendError, not KeyError."""
    cfg = Config.load(tmp_path / "config.json")
    with pytest.raises(BackendError, match="unknown profile"):
        create("nope", cfg)


def test_public_api_exports_registry_helpers() -> None:
    """L18: ``__all__`` must advertise the registry helpers callers import."""
    from nadare.core import backends

    for name in ("supports_media", "unavailable"):
        assert name in backends.__all__
        assert hasattr(backends, name)


def test_create_returns_instance_with_context(tmp_path: Path) -> None:
    register("dummy-test", DummyBackend)
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("p", backend="dummy-test")

    backend = create("p", cfg)

    assert isinstance(backend, DummyBackend)
    assert backend.ctx.name == "p"
    assert backend.ctx.profile is cfg.profile("p")
    assert backend.ctx.cancel.cancelled is False


def test_context_data_dir_and_profile_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles_root = tmp_path / "profiles"
    monkeypatch.setattr(paths, "profiles_dir", lambda: profiles_root)

    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("p")
    ctx = BackendContext(name="p", profile=cfg.profile("p"), config=cfg)

    data_dir = ctx.data_dir()
    assert data_dir == profiles_root / "p"
    assert data_dir.is_dir()

    nested = ctx.profile_dir("prefix", "drive_c")
    assert nested == data_dir / "prefix" / "drive_c"
    assert nested.is_dir()


def test_context_data_dir_refuses_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_root = tmp_path / "profiles"
    monkeypatch.setattr(paths, "profiles_dir", lambda: profiles_root)

    cfg = Config.load(tmp_path / "config.json")
    ctx = BackendContext(name="../escape", profile={}, config=cfg)

    with pytest.raises(BackendError):
        ctx.data_dir()
    assert not (tmp_path / "escape").exists()


def test_context_data_dir_refuses_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_root = tmp_path / "profiles"
    monkeypatch.setattr(paths, "profiles_dir", lambda: profiles_root)

    outside = tmp_path / "outside"
    cfg = Config.load(tmp_path / "config.json")
    ctx = BackendContext(name=str(outside), profile={}, config=cfg)

    with pytest.raises(BackendError):
        ctx.data_dir()
    assert not outside.exists()


def test_context_data_dir_permits_symlinked_profile_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (NEW-2): relocating a profile to another disk via a symlink
    must keep working; the *name* is the untrusted input, not the final path."""
    profiles_root = tmp_path / "profiles"
    profiles_root.mkdir(parents=True)
    other_disk = tmp_path / "other-disk"
    other_disk.mkdir()
    (profiles_root / "p").symlink_to(other_disk, target_is_directory=True)

    monkeypatch.setattr(paths, "profiles_dir", lambda: profiles_root)
    cfg = Config.load(tmp_path / "config.json")
    ctx = BackendContext(name="p", profile={}, config=cfg)

    data_dir = ctx.data_dir()
    assert data_dir == profiles_root / "p"
    assert data_dir.resolve() == other_disk.resolve()
    assert data_dir.is_dir()

    nested = ctx.profile_dir("prefix")
    assert nested == profiles_root / "p" / "prefix"
    assert nested.resolve() == other_disk / "prefix"
    assert (other_disk / "prefix").is_dir()


def test_context_set_persists_profile_value(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.add_profile("p")
    cfg.save()
    ctx = BackendContext(name="p", profile=cfg.profile("p"), config=cfg)

    ctx.set("runner", "/opt/proton/GE")

    assert ctx.profile["runner"] == "/opt/proton/GE"
    reloaded = Config.load(path)
    assert reloaded.profile("p")["runner"] == "/opt/proton/GE"


def test_context_set_is_thread_safe(tmp_path: Path) -> None:
    """06 #4: concurrent worker set() must not corrupt or lose the config."""
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.add_profile("p")
    cfg.save()
    ctx = BackendContext(name="p", profile=cfg.profile("p"), config=cfg)

    workers = 8
    per_worker = 25
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def run(worker: int) -> None:
        try:
            barrier.wait()
            for index in range(per_worker):
                ctx.set(f"k{worker}_{index}", index)
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(worker,)) for worker in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    reloaded = Config.load(path)
    profile = reloaded.profile("p")
    for worker in range(workers):
        for index in range(per_worker):
            assert profile[f"k{worker}_{index}"] == index


def test_optional_defaults(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("p", backend="dummy-test")
    backend = DummyBackend(BackendContext("p", cfg.profile("p"), cfg))

    with pytest.raises(BackendUnsupported):
        backend.login()
    with pytest.raises(BackendUnsupported):
        backend.uninstall()
    with pytest.raises(BackendUnsupported):
        backend.set_remember(True)
    with pytest.raises(BackendUnsupported):
        backend.logout()
    assert backend.verify() == {"verified": False, "message": "nothing to verify"}


def test_supports_remember_me_reports_capabilities_safely() -> None:
    from nadare.core.backends import supports_remember_me

    assert supports_remember_me("stove-czn") is True
    assert supports_remember_me("script") is False
    assert supports_remember_me("no-such-backend-xyz") is False


def test_supports_login_reports_capabilities_safely() -> None:
    from nadare.core.backends import supports_login

    assert supports_login("stove-czn") is True
    assert supports_login("fgo-lepton") is False
    assert supports_login("arknights-yostar") is False
    assert supports_login("stellasora-yostar") is False
    assert supports_login("wine") is False
    assert supports_login("no-such-backend-xyz") is False


def test_backend_error_fields() -> None:
    err = BackendError("boom", code=7)
    assert err.message == "boom"
    assert err.code == 7
    assert str(err) == "boom"


def test_kinds_are_sorted_and_unique() -> None:
    ks = kinds()
    assert ks == sorted(set(ks))
    # The builtin registry advertises these kinds even before import succeeds.
    for builtin in (
        "stove-czn",
        "fgo-lepton",
        "wine",
        "steam",
        "script",
        "waydroid",
        "arknights-yostar",
        "stellasora-yostar",
    ):
        assert builtin in ks


def test_every_builtin_backend_imports_and_is_bundled():
    """Each registry entry imports from source and is covered by the
    PyInstaller spec, which collects ``nadare.backends`` wholesale (a hand-kept
    list once missed arknights/stellasora and broke them in the AppImage)."""
    import importlib

    from nadare.core import backends

    spec = (Path(__file__).resolve().parents[1] / "packaging" / "nadare.spec").read_text()
    assert 'collect_submodules("nadare.backends")' in spec
    for kind, target in backends._BUILTINS.items():
        mod_name, _, attr = target.partition(":")
        assert mod_name.startswith("nadare.backends."), kind
        assert getattr(importlib.import_module(mod_name), attr), kind
