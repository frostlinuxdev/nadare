# SPDX-License-Identifier: GPL-3.0-or-later
"""#1452 step 3c: Yostar's own launcher route is retired (DECISIONS 2026-11-17).

Arknights and Stella Sora always install the game themselves from the manifest. With
no manifest (the index turned off, nothing configured) they refuse Install and Update
with a clear message, like the other direct games, and Yostar's installer or launcher
never runs. An installed game still plays: launching needs no manifest. A b25 profile
that still carries the old installer keys installs and plays as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends import winegame as wg
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.stellasora import StellaSoraBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import directdl, yostar
from nadare.core.backends import BackendError


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(wg.runners, "installed_pinned", lambda family="dwproton": None)

    def offline(*_a, **_k):
        raise wg.runners.RunnerError("offline (no downloads in tests)")

    monkeypatch.setattr(wg.runners, "ensure_pinned", offline)
    monkeypatch.setattr(WineGameBackend, "_ensure_direct_prefix", lambda self: None)


@pytest.fixture
def index_off(monkeypatch):
    """No manifest anywhere: the published index disabled, nothing in the profile."""
    monkeypatch.delenv("YOSTAR_MANIFEST_INDEX", raising=False)
    monkeypatch.setattr(yostar.hosting, "yostar_index_url", lambda: "")


@pytest.fixture
def no_launcher_route(monkeypatch):
    """Any step of the old route fails the test."""

    def forbidden(*_a, **_k):
        pytest.fail("Yostar's installer/launcher route must never run (#1452)")

    for name in (
        "_download_installer",
        "_run_installer",
        "_install_launcher_directly",
        "require_installer_url",
        "build_launch",
    ):
        monkeypatch.setattr(WineGameBackend, name, forbidden)
    monkeypatch.setattr(
        directdl, "sync", lambda *a, **k: pytest.fail("nothing may download without a manifest")
    )


KINDS = [
    (ArknightsBackend, ak.KIND, "Arknights", "Arknights.exe"),
    (StellaSoraBackend, StellaSoraBackend.kind, "Stella Sora", "StellaSora.exe"),
]


def _backend(cls, kind, tmp_path: Path, profile: dict | None = None):
    return cls(make_context(tmp_path, kind, profile or {}, "g"))


@pytest.mark.parametrize(("cls", "kind", "name", "exe"), KINDS)
def test_always_direct(cls, kind, name, exe, tmp_path, index_off) -> None:
    backend = _backend(cls, kind, tmp_path)
    assert backend.direct_mode() is True
    assert backend.status().data["mode"] == "direct"


@pytest.mark.parametrize(("cls", "kind", "name", "exe"), KINDS)
def test_install_without_a_manifest_refuses_clearly(
    cls, kind, name, exe, tmp_path, index_off, no_launcher_route, monkeypatch
) -> None:
    backend = _backend(cls, kind, tmp_path)
    # The refusal comes first: no runner download, no prefix, nothing recorded.
    monkeypatch.setattr(
        cls, "_ensure_default_runner", lambda self, **kw: pytest.fail("runner set up")
    )
    with pytest.raises(BackendError) as caught:
        backend.install()
    assert ak.INSTALLED_PREFIX_KEY not in backend.ctx.profile
    message = str(caught.value)
    assert name in message
    assert "manifest" in message.lower() or "index" in message.lower()
    assert "launcher" not in message.lower()
    assert not backend.install_dir().exists() or not any(backend.install_dir().iterdir())


@pytest.mark.parametrize(("cls", "kind", "name", "exe"), KINDS)
def test_update_without_a_manifest_refuses_clearly(
    cls, kind, name, exe, tmp_path, index_off, no_launcher_route
) -> None:
    backend = _backend(cls, kind, tmp_path)
    write_exe(backend.install_dir() / exe, "MZ")
    with pytest.raises(BackendError) as caught:
        backend.update()
    assert name in str(caught.value)
    assert (backend.install_dir() / exe).read_bytes() == b"MZ"


@pytest.mark.parametrize(("cls", "kind", "name", "exe"), KINDS)
def test_an_installed_game_still_plays_without_a_manifest(
    cls, kind, name, exe, tmp_path, index_off, no_launcher_route, monkeypatch
) -> None:
    backend = _backend(cls, kind, tmp_path)
    write_exe(backend.install_dir() / exe, "MZ")
    monkeypatch.setattr(cls, "_ensure_default_runner", lambda self, **kw: None)
    launched: list[str] = []
    monkeypatch.setattr(cls, "_launch_direct", lambda self: launched.append("direct") or 0)
    assert backend.launch() == 0
    assert launched == ["direct"]


@pytest.mark.parametrize(("cls", "kind", "name", "exe"), KINDS)
def test_not_installed_launch_says_so_without_the_launcher(
    cls, kind, name, exe, tmp_path, index_off, no_launcher_route
) -> None:
    backend = _backend(cls, kind, tmp_path)
    with pytest.raises(BackendError, match="not found|not installed"):
        backend.launch()


def test_no_installer_options_are_offered() -> None:
    for cls in (ArknightsBackend, StellaSoraBackend):
        keys = {option.key for option in cls.options}
        assert not keys & {"installer_url", "installer_sha256", "installer_args"}, cls
        exe = next(option for option in cls.options if option.key == "exe_name")
        assert "launcher" not in exe.label.lower()


def test_arknights_keeps_its_media_and_manifest_options() -> None:
    keys = {option.key for option in ArknightsBackend.options}
    assert {"manifest_index", "manifest_file", "media_file", "secure_mode"} <= keys


def test_the_sandbox_follows_the_profile_even_with_the_index_off(tmp_path, index_off) -> None:
    """Before, no manifest meant Yostar's launcher and so no Sandbox; now there is no such
    route, so the Sandbox setting applies as it does for every direct game."""
    # The suite turns the Sandbox's default off (gamesandbox.DEFAULT_ON), so choose it.
    on = _backend(ArknightsBackend, ak.KIND, tmp_path, {"secure_mode": True})
    off = _backend(ArknightsBackend, ak.KIND, tmp_path / "off", {"secure_mode": False})
    assert on.secure_mode_enabled() is True
    assert off.secure_mode_enabled() is False


def test_a_b25_profile_with_installer_keys_installs_directly(
    tmp_path, monkeypatch, no_launcher_route
) -> None:
    """Upgrade: old keys stay in the profile, unused; nothing about them fails."""
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            {
                "game_tag": "Arknights_EN",
                "version": "1.0",
                "base_url": "https://cdn.yo-star.com/assets",
                "generated_at": "",
                "files": [{"path": "Arknights.exe", "size": 2, "crc64": yostar.crc64(b"MZ")}],
            }
        ),
        encoding="utf-8",
    )
    profile = {
        "manifest_file": str(manifest),
        "installer_url": "https://example.com/Arknights_EN_Gamelauncher-setup.exe",
        "installer_sha256": "0" * 64,
        "installer_args": "/S",
    }
    backend = _backend(ArknightsBackend, ak.KIND, tmp_path, profile)
    synced: list[str] = []

    def fake_sync(plan, dest, **kwargs):
        synced.append(plan.version)
        write_exe(Path(dest) / "Arknights.exe", "MZ")
        return {"downloaded": 1, "skipped": 0}

    monkeypatch.setattr(directdl, "sync", fake_sync)
    backend.install()
    assert synced == ["1.0"]
    assert backend.status().state == "installed"
    assert backend.ctx.profile["installer_url"].endswith("setup.exe")  # left alone
