# SPDX-License-Identifier: GPL-3.0-or-later
"""Plain-language error messages for the failures reported in the UX audit."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn import update as czn_update
from nadare.backends.czn.backend import CznBackend
from nadare.backends.fgo import installer, lepton
from nadare.backends.fgo.backend import FgoLeptonBackend
from nadare.backends.wine import WineBackend
from nadare.core import download, runners, yostar
from nadare.core.backends import BackendError
from nadare.core.events import Cancelled


def test_webview2_mismatch_gives_a_gui_action_not_an_env_var(tmp_path, monkeypatch):
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "p")}))
    inst = tmp_path / "installer.exe"
    inst.write_bytes(b"MZ")
    monkeypatch.delenv("NADARE_WEBVIEW2_SHA256", raising=False)
    monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)
    monkeypatch.setattr(CznBackend, "_webview2_index_pins", staticmethod(lambda: set()))

    with pytest.raises(BackendError) as exc:
        backend._verify_webview2(inst)

    message = exc.value.message
    assert "NADARE_WEBVIEW2_SHA256" not in message
    assert "SHA-256" not in message
    assert "Install again" in message


def test_webview2_unpinned_message_has_no_internal_setting(tmp_path, monkeypatch):
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "p")}))
    inst = tmp_path / "installer.exe"
    inst.write_bytes(b"MZ")
    monkeypatch.delenv("NADARE_WEBVIEW2_SHA256", raising=False)
    monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", None)

    with pytest.raises(BackendError) as exc:
        backend._verify_webview2(inst)

    message = exc.value.message
    assert "NADARE_WEBVIEW2_SHA256" not in message
    assert "webview2_sha256" not in message
    assert "Install again" in message


def test_missing_czn_game_files_are_not_reported_as_network(tmp_path, monkeypatch):
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "p")}))
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    monkeypatch.setattr(backend, "_run_module", lambda *a, **k: 5)

    with pytest.raises(BackendError) as exc:
        backend.update()

    assert exc.value.code == 5
    assert "missing or incomplete" in exc.value.message
    assert "network" not in exc.value.message.lower()
    assert "Install" in exc.value.message


def test_czn_updater_returns_its_own_code_for_a_missing_game_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(czn_update, "GAME_DIR", tmp_path / "missing")
    monkeypatch.setattr(czn_update, "PREFIX", tmp_path / "prefix")

    assert czn_update.main(["check"]) == 5
    assert "game dir not found" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("error", "code", "text"),
    [
        (download.DownloadError("cannot reach github"), 4, "GitHub"),
        (download.IntegrityError("sha256 mismatch"), 9, "damaged or changed"),
        (runners.RunnerError("cannot query CachyOS/x: offline"), 4, "GitHub"),
        (
            runners.RunnerError("refusing runner tarball from untrusted host: x"),
            1,
            "could not install",
        ),
    ],
)
def test_czn_runner_failures_map_to_user_messages(tmp_path, monkeypatch, error, code, text):
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "p")}))

    def boom(*a, **k):
        raise error

    monkeypatch.setattr(runners, "install", boom)

    with pytest.raises(BackendError) as exc:
        backend._download_runner("dwproton", False)

    assert exc.value.code == code
    assert text in exc.value.message
    assert "STOVE" not in exc.value.message


def test_czn_runner_download_runs_in_process_through_core(tmp_path, monkeypatch):
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "p")}))
    seen = {}

    def fake_install(rid, *, latest, emit, cancel):
        seen.update(rid=rid, latest=latest, emit=emit, cancel=cancel)
        return tmp_path / "dwproton-11.0-15-x86_64"

    monkeypatch.setattr(runners, "install", fake_install)
    monkeypatch.setattr(
        backend, "_run_module", lambda *a, **k: pytest.fail("runner install spawned a process")
    )

    assert backend._download_runner("dwproton", True) == tmp_path / "dwproton-11.0-15-x86_64"
    assert seen["rid"] == "dwproton" and seen["latest"] is True
    assert seen["cancel"] is backend.ctx.cancel


def test_czn_runner_cancel_passes_through(tmp_path, monkeypatch):
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "p")}))

    def cancelled(*a, **k):
        raise Cancelled("download cancelled")

    monkeypatch.setattr(runners, "install", cancelled)

    with pytest.raises(Cancelled):
        backend._download_runner("dwproton", False)


class _FgoAdapter:
    context = "fgo"

    def set_repo(self, repo):
        pass

    def check_host(self):
        return {"ok": True, "output": ""}

    def selinux_root_command(self):
        return ""

    def cmd_install(self, **kwargs):
        return ["install.sh"]

    def cmd_update(self, force=False):
        return ["update"]


def test_fgo_install_failure_is_plain(tmp_path, monkeypatch):
    backend = FgoLeptonBackend(
        make_context(tmp_path, "fgo-lepton", {"accept_account_notice": True})
    )
    backend.adapter = _FgoAdapter()  # type: ignore[assignment]
    monkeypatch.setattr(installer, "ensure_installer", lambda adapter, **k: tmp_path)
    monkeypatch.setattr(backend, "_stream", lambda cmd: 1)

    with pytest.raises(BackendError) as exc:
        backend.install()

    message = exc.value.message
    assert "install.sh" not in message
    assert "exit" not in message.lower()
    assert "did not finish" in message
    assert "try again" in message.lower()


def test_fgo_update_failure_is_plain(tmp_path, monkeypatch):
    backend = FgoLeptonBackend(
        make_context(tmp_path, "fgo-lepton", {"accept_account_notice": True})
    )
    backend.adapter = _FgoAdapter()  # type: ignore[assignment]
    monkeypatch.setattr(backend, "_stream", lambda cmd: 1)

    with pytest.raises(BackendError) as exc:
        backend.update()

    message = exc.value.message
    assert "exit" not in message.lower()
    assert "did not finish" in message
    assert "try again" in message.lower()


def test_lepton_spawn_failure_hides_the_internal_path(tmp_path):
    adapter = lepton.LeptonFgo(prefix=tmp_path / "p", config_dir=tmp_path / "c")
    tool = tmp_path / "tool.sh"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o644)

    with pytest.raises(lepton.LeptonError) as exc:
        adapter.run([str(tool)])

    message = str(exc.value)
    assert str(tmp_path) not in message
    assert "could not run" not in message
    assert "Install again" in message


def test_yostar_download_failure_is_plain_not_raw_urllib(tmp_path, monkeypatch):
    payload = b"abc"
    manifest = yostar.Manifest(
        game_tag="t",
        version="1",
        base_url="https://cdn.yo-star.com/assets",
        generated_at="",
        files=(yostar.ManifestFile("bin/game.exe", len(payload), yostar.crc64(payload)),),
    )

    def offline(*_a, **_k):
        raise download.DownloadError(
            "cannot reach https://cdn.yo-star.com/game/bin/game.exe: "
            "<urlopen error [Errno -3] Temporary failure in name resolution>"
        )

    monkeypatch.setattr(yostar.download, "stream_download", offline)

    with pytest.raises(yostar.ManifestError) as exc:
        yostar.sync(manifest, tmp_path, retries=1)

    message = str(exc.value)
    assert "https://" not in message
    assert "urlopen" not in message
    assert "internet connection" in message.lower()
    assert "try again" in message.lower()


def test_arknights_install_runner_failure_is_not_a_wine_binary_hint(tmp_path, monkeypatch):
    backend = ArknightsBackend(
        make_context(
            tmp_path,
            ak.KIND,
            {"installer_url": "https://launcher-pkg-ark-en.yo-star.com/a-setup.exe"},
        )
    )
    monkeypatch.setattr(ak.shutil, "which", lambda name: None)
    monkeypatch.setattr(ak.runners, "installed_pinned", lambda family="dwproton": None)

    def offline(*_a, **_k):
        raise ak.runners.RunnerError("offline")

    monkeypatch.setattr(ak.runners, "ensure_pinned", offline)
    monkeypatch.setattr(ak, "stream", lambda *a, **k: pytest.fail("must not start Wine"))

    with pytest.raises(BackendError) as exc:
        backend.install()

    message = exc.value.message
    assert "Proton runner" in message
    assert "internet" in message.lower()
    assert "wine_binary" not in message


def test_arknights_installer_failure_offers_a_gui_runner_choice(tmp_path, monkeypatch):
    backend = ArknightsBackend(
        make_context(
            tmp_path,
            ak.KIND,
            {
                "installer_url": "https://launcher-pkg-ark-en.yo-star.com/a-setup.exe",
                "wine_binary": "/usr/bin/wine",
            },
        )
    )
    installer_exe = tmp_path / "setup.exe"
    installer_exe.write_bytes(b"MZ")
    monkeypatch.setattr(ArknightsBackend, "_ensure_prefix", lambda self: None)
    monkeypatch.setattr(ArknightsBackend, "_download_installer", lambda self, url: installer_exe)
    monkeypatch.setattr(ak, "stream", lambda *a, **k: 0)

    with pytest.raises(BackendError) as exc:
        backend.install()

    message = exc.value.message
    assert "Proton-CachyOS" in message
    assert "Settings" in message
    assert "direct download" not in message


def test_wine_install_and_missing_exe_point_at_settings(tmp_path):
    backend = WineBackend(make_context(tmp_path, "wine", {}))

    with pytest.raises(BackendError) as exc:
        backend.install()
    assert "install_dir" not in exc.value.message
    assert "Settings" in exc.value.message
    assert "Game options" in exc.value.message

    with pytest.raises(BackendError) as exc:
        backend._target()
    assert "exe_name" not in exc.value.message
    assert "launch_exe" not in exc.value.message
    assert "Settings" in exc.value.message
    assert "Game options" in exc.value.message


def test_has_display_needs_x11_or_wayland():
    from nadare.core import graphics

    assert graphics.has_display({"DISPLAY": ":0"})
    assert graphics.has_display({"WAYLAND_DISPLAY": "wayland-0"})
    assert not graphics.has_display({})
    assert not graphics.has_display({"DISPLAY": "", "WAYLAND_DISPLAY": ""})


def test_webview2_install_fails_fast_without_a_display(tmp_path, monkeypatch):
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "p")}))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(czn_backend, "WEBVIEW2_SHA256", "0" * 64)
    monkeypatch.setattr(backend, "_webview2_installed", lambda: False)

    def _no_download(*a, **k):
        raise AssertionError("downloaded without a display")

    monkeypatch.setattr(backend, "_download_webview2", _no_download)
    with pytest.raises(BackendError) as exc:
        backend._ensure_webview2()
    assert "No display found" in exc.value.message
