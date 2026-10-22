# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the diagnostic sweep (#54, #56, #62, #88, #92, #194)."""

from __future__ import annotations

import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from _backend_helpers import make_context

from nadare.core.events import Cancel, Cancelled


# ---------------------------------------------------------------------------
# #88: CZN parse/QR status checks
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses) -> None:
        self.headers: dict[str, str] = {}
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def post(self, url, json=None, headers=None, timeout=None):
        return self._next("POST", url, json=json, headers=headers)

    def get(self, url, params=None, headers=None, timeout=None):
        return self._next("GET", url, params=params, headers=headers)


def _stove(monkeypatch, responses=()):
    from nadare.backends.czn import auth

    fake = _Session(responses)
    monkeypatch.setattr(auth, "creq", SimpleNamespace(Session=lambda *a, **k: fake))
    # A caller_detail avoids writing the state file while constructing the client.
    return auth.StoveAuth({"caller_detail": "0" * 40}), fake



# ---------------------------------------------------------------------------
# #92: a free-form message is not a login state
# ---------------------------------------------------------------------------
def test_login_qr_does_not_treat_message_success_as_a_state(monkeypatch, tmp_path):
    import base64

    from nadare.backends.czn import auth

    stove, _ = _stove(monkeypatch)
    image = base64.b64encode(b"PNG").decode()
    monkeypatch.setattr(
        stove, "qr_create", lambda: {"session": "s", "url": "u", "image": image}
    )
    monkeypatch.setattr(
        stove, "qr_status", lambda session: {"code": 0, "message": "success", "value": {}}
    )
    monkeypatch.setattr(auth.time, "sleep", lambda _s: None)
    signed: list[str] = []
    monkeypatch.setattr(stove, "signin_qr", lambda session: signed.append(session))

    with pytest.raises(RuntimeError, match="timed out"):
        stove.login_qr(timeout_s=0.05, png_path=tmp_path / "qr.png")
    assert signed == []


@pytest.mark.parametrize("state", ["success", "complete", "completed", "done"])
def test_login_qr_signs_in_on_a_real_value_status(monkeypatch, tmp_path, state):
    import base64

    stove, _ = _stove(monkeypatch)
    image = base64.b64encode(b"PNG").decode()
    monkeypatch.setattr(
        stove, "qr_create", lambda: {"session": "s", "url": "u", "image": image}
    )
    monkeypatch.setattr(stove, "qr_status", lambda session: {"value": {"status": state}})
    signed: list[str] = []
    monkeypatch.setattr(stove, "signin_qr", lambda session: signed.append(session))
    monkeypatch.setattr(stove, "_persist_launcher", lambda: None)

    stove.login_qr(timeout_s=5, png_path=tmp_path / "qr.png")
    assert signed == ["s"]


# ---------------------------------------------------------------------------
# #56: install() wraps a check_host LeptonError
# ---------------------------------------------------------------------------
def test_fgo_install_wraps_a_check_host_lepton_error(tmp_path, monkeypatch):
    from nadare.backends.fgo import installer
    from nadare.backends.fgo.backend import FgoLeptonBackend
    from nadare.backends.fgo.lepton import LeptonError

    class _BoomAdapter:
        context = "fgo"

        def set_repo(self, repo):
            pass

        def check_host(self):
            raise LeptonError("host check exploded")

    backend = FgoLeptonBackend(
        make_context(tmp_path, "fgo-lepton", {"accept_account_notice": True})
    )
    backend.adapter = _BoomAdapter()  # type: ignore[assignment]
    monkeypatch.setattr(installer, "ensure_installer", lambda adapter, **k: tmp_path)

    from nadare.core.backends import BackendError

    with pytest.raises(BackendError, match="host check exploded"):
        backend.install()


# ---------------------------------------------------------------------------
# #54: run()/verify()/check_host() honour a cancel token
# ---------------------------------------------------------------------------
def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.02)
    return False


def _sleeping_child(pid_file: Path) -> list[str]:
    code = (
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    return [sys.executable, "-c", code]


def test_run_cancel_stops_a_silent_child(tmp_path, monkeypatch):
    from nadare.backends.fgo import lepton
    from nadare.core import sandbox

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    adapter = lepton.LeptonFgo(prefix=tmp_path / "p", config_dir=tmp_path / "c")
    pid_file = tmp_path / "pid"
    cancel = Cancel()

    def _cancel_when_started() -> None:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.cancel()

    threading.Thread(target=_cancel_when_started, daemon=True).start()
    with pytest.raises(Cancelled):
        adapter.run(_sleeping_child(pid_file), timeout=30, cancel=cancel)
    assert pid_file.exists()
    assert _wait_gone(int(pid_file.read_text()))


def test_verify_and_check_host_forward_the_cancel_token(tmp_path, monkeypatch):
    from nadare.backends.fgo import lepton

    adapter = lepton.LeptonFgo(prefix=tmp_path / "p", config_dir=tmp_path / "c")
    seen: list[object] = []

    def fake_run(cmd, timeout=60, cancel=None):
        seen.append(cancel)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(adapter, "run", fake_run)
    adapter.check_host_sh.parent.mkdir(parents=True, exist_ok=True)
    adapter.check_host_sh.write_text("#!/bin/sh\n")
    cancel = Cancel()
    adapter.verify(cancel=cancel)
    adapter.check_host(cancel=cancel)
    assert seen == [cancel, cancel]


def test_backend_verify_passes_the_context_cancel(tmp_path):
    from nadare.backends.fgo.backend import FgoLeptonBackend

    backend = FgoLeptonBackend(make_context(tmp_path, "fgo-lepton", {}))
    seen: list[object] = []
    backend.adapter.verify = lambda release=None, cancel=None: (  # type: ignore[method-assign]
        seen.append(cancel) or {"returncode": 0, "output": "ok"}
    )
    backend.verify()
    assert seen == [backend.ctx.cancel]


# ---------------------------------------------------------------------------
# #62: switching profiles stops the previous game's elapsed clock
# ---------------------------------------------------------------------------
def test_set_profile_stops_the_elapsed_clock():
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    from nadare.gui.game_view import GameSnapshot, GameView

    app = QApplication.instance() or QApplication([])
    view = GameView()
    try:
        view.set_busy("install")
        assert view._timer.isActive()
        view.set_profile("beta", {"backend": "wine", "title": "Beta"}, GameSnapshot())
        assert not view._timer.isActive()
        assert view._started_at == 0.0
        assert view.elapsed_label.text() == ""
    finally:
        view.setParent(None)
        view.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# #194: opt-in forged-chain TLS diagnostic
# ---------------------------------------------------------------------------
class _Connected:
    def close(self) -> None:
        pass


def test_check_forged_chain_hosts_classifies_rejection_and_connection():
    from nadare.core import tls

    def opener(url, timeout=10):
        if url == tls.DIAGNOSTIC_HOSTS[0]:
            raise urllib.error.URLError(
                ssl.SSLCertVerificationError(1, "certificate verify failed")
            )
        return _Connected()

    result = tls.check_forged_chain_hosts(opener=opener)
    assert result == [(tls.DIAGNOSTIC_HOSTS[0], True), (tls.DIAGNOSTIC_HOSTS[1], False)]


def test_check_forged_chain_hosts_marks_a_plain_network_error_unchecked():
    from nadare.core import tls

    def opener(url, timeout=10):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    result = tls.check_forged_chain_hosts(opener=opener)
    assert [host for host, _ in result] == list(tls.DIAGNOSTIC_HOSTS)
    assert all(rejected is None for _, rejected in result)


def test_doctor_tls_flag_is_opt_in_and_parsed():
    from nadare.app import build_parser

    assert build_parser().parse_args(["doctor"]).tls is False
    assert build_parser().parse_args(["doctor", "--tls"]).tls is True


def _patch_doctor_probes(monkeypatch):
    from nadare.core import backends, graphics, process, runners, secrets

    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(process, "which", lambda name, **k: None)
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(backends, "unavailable", list)
    monkeypatch.setattr(backends, "kinds", lambda: ["wine"])
    monkeypatch.setattr(secrets, "keyring_diagnosis", lambda: (True, "test"))


def test_doctor_never_checks_tls_without_the_flag(monkeypatch, capsys):
    from nadare.app import run_cli
    from nadare.core import tls

    _patch_doctor_probes(monkeypatch)
    called: list[bool] = []
    monkeypatch.setattr(
        tls, "check_forged_chain_hosts", lambda **k: called.append(True) or []
    )

    assert run_cli(["doctor"]) == 0
    assert called == []
    assert "rejected (good)" not in capsys.readouterr().out


def test_doctor_tls_prints_hosts_and_remediation(monkeypatch, capsys):
    from nadare.app import run_cli
    from nadare.core import tls

    _patch_doctor_probes(monkeypatch)
    monkeypatch.setattr(
        tls,
        "check_forged_chain_hosts",
        lambda **k: [(tls.DIAGNOSTIC_HOSTS[0], True), (tls.DIAGNOSTIC_HOSTS[1], False)],
    )

    assert run_cli(["doctor", "--tls"]) == 0
    out = capsys.readouterr().out
    assert f"{tls.DIAGNOSTIC_HOSTS[0]}: rejected (good)" in out
    assert "ACCEPTED" in out
    assert "emSign" in out and "Comodo_AAA_Services_root" in out
    assert "update-ca-certificates --fresh" in out
    assert "EM DV TLS CA - G3B" in out
    assert "--use-openssl-ca" in out
