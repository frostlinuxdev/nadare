# SPDX-License-Identifier: GPL-3.0-or-later
"""The installer canary warns when a pinned NTE or Endfield installer goes stale."""

from __future__ import annotations

import gzip
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_installer_pins.py"


def _canary():
    spec = importlib.util.spec_from_file_location("installer_pins_canary", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fake_head(status: int = 200, length: int | None = None):
    calls: list[str] = []

    def head(url: str) -> tuple[int, int | None]:
        calls.append(url)
        return status, length

    return head, calls


def fake_json(data):
    calls: list[str] = []

    def get_json(url: str):
        calls.append(url)
        return data

    return get_json, calls


def test_the_nte_installer_size_is_a_positive_int() -> None:
    canary = _canary()
    assert isinstance(canary.nte_launcher.INSTALLER_SIZE, int)
    assert canary.nte_launcher.INSTALLER_SIZE > 0


def test_an_unchanged_nte_installer_is_ok() -> None:
    canary = _canary()
    head, calls = fake_head(200, canary.nte_launcher.INSTALLER_SIZE)
    assert canary.check_nte(head) == []
    assert calls == [canary.nte_launcher.INSTALLER_URL]


def test_a_changed_nte_installer_size_is_a_problem() -> None:
    canary = _canary()
    pinned = canary.nte_launcher.INSTALLER_SIZE
    head, _calls = fake_head(200, pinned + 1)
    problems = canary.check_nte(head)
    assert len(problems) == 1
    assert "changed size" in problems[0]
    assert str(pinned + 1) in problems[0] and str(pinned) in problems[0]


@pytest.mark.parametrize("code", [404, 410])
def test_a_gone_nte_installer_url_is_a_problem(code: int) -> None:
    canary = _canary()
    head, _calls = fake_head(code, None)
    problems = canary.check_nte(head)
    assert len(problems) == 1
    assert "gone" in problems[0]
    assert f"HTTP {code}" in problems[0]
    assert "INSTALLER_SHA256" in problems[0] and "INSTALLER_SIZE" in problems[0]


def test_a_200_without_a_content_length_is_only_a_note(
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = _canary()
    head, _calls = fake_head(200, None)
    assert canary.check_nte(head) == []
    assert "installer pins" in capsys.readouterr().err


def test_an_nte_server_error_is_only_a_note(capsys: pytest.CaptureFixture[str]) -> None:
    canary = _canary()
    head, _calls = fake_head(503, None)
    assert canary.check_nte(head) == []
    assert "installer pins" in capsys.readouterr().err


def test_an_nte_network_error_is_only_a_note(capsys: pytest.CaptureFixture[str]) -> None:
    canary = _canary()

    def head(url: str) -> tuple[int, int | None]:
        raise TimeoutError("offline")

    assert canary.check_nte(head) == []
    assert "installer pins" in capsys.readouterr().err


def test_an_endfield_installer_on_an_allowed_host_is_ok() -> None:
    canary = _canary()
    get_json, calls = fake_json({"exe_url": "https://launcher.gryphline.com/setup.exe"})
    assert canary.check_endfield(get_json) == []
    assert calls == [canary.endfield_shim.LAUNCHER_API]


def test_an_endfield_installer_on_an_unknown_host_is_a_problem() -> None:
    canary = _canary()
    get_json, _calls = fake_json({"exe_url": "https://evil.example/setup.exe"})
    problems = canary.check_endfield(get_json)
    assert len(problems) == 1
    assert "evil.example" in problems[0]
    assert "ALLOWED_INSTALLER_HOSTS" in problems[0]


@pytest.mark.parametrize("data", [{}, {"exe_url": None}, {"exe_url": 123}, [], "nope"])
def test_an_endfield_api_without_an_exe_url_is_a_problem(data) -> None:
    canary = _canary()
    get_json, _calls = fake_json(data)
    problems = canary.check_endfield(get_json)
    assert len(problems) == 1
    assert "no exe_url" in problems[0]


def test_an_endfield_network_error_is_only_a_note(capsys: pytest.CaptureFixture[str]) -> None:
    canary = _canary()

    def get_json(url: str):
        raise TimeoutError("offline")

    assert canary.check_endfield(get_json) == []
    assert "installer pins" in capsys.readouterr().err


def test_an_endfield_json_error_is_only_a_note(capsys: pytest.CaptureFixture[str]) -> None:
    canary = _canary()

    def get_json(url: str):
        raise ValueError("not json")

    assert canary.check_endfield(get_json) == []
    assert "installer pins" in capsys.readouterr().err


def test_main_is_ok_when_both_installers_match(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = _canary()
    head, _calls = fake_head(200, canary.nte_launcher.INSTALLER_SIZE)
    get_json, _jcalls = fake_json({"exe_url": "https://hg-cdn.com/setup.exe"})
    monkeypatch.setattr(canary, "_head", head)
    monkeypatch.setattr(canary, "_get_json", get_json)
    assert canary.main([]) == 0
    assert "installer pins: ok" in capsys.readouterr().out


def test_main_exits_4_for_a_gone_nte_installer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = _canary()
    head, _calls = fake_head(404, None)
    get_json, _jcalls = fake_json({"exe_url": "https://hg-cdn.com/setup.exe"})
    monkeypatch.setattr(canary, "_head", head)
    monkeypatch.setattr(canary, "_get_json", get_json)
    assert canary.main([]) == 4
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("CANARY:")]
    assert len(lines) == 1
    assert "NTE installer" in lines[0]


def test_main_exits_4_for_a_moved_endfield_installer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = _canary()
    head, _calls = fake_head(200, canary.nte_launcher.INSTALLER_SIZE)
    get_json, _jcalls = fake_json({"exe_url": "https://evil.example/setup.exe"})
    monkeypatch.setattr(canary, "_head", head)
    monkeypatch.setattr(canary, "_get_json", get_json)
    assert canary.main([]) == 4
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("CANARY:")]
    assert len(lines) == 1
    assert "Endfield" in lines[0]


def test_main_treats_notes_as_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = _canary()

    def head(url: str) -> tuple[int, int | None]:
        raise TimeoutError("offline")

    def get_json(url: str):
        raise TimeoutError("offline")

    monkeypatch.setattr(canary, "_head", head)
    monkeypatch.setattr(canary, "_get_json", get_json)
    assert canary.main([]) == 0
    captured = capsys.readouterr()
    assert "CANARY:" not in captured.out
    assert "installer pins" in captured.err


def test_a_gzip_compressed_api_body_is_decoded() -> None:
    canary = _canary()
    body = '{"exe_url": "https://hg-cdn.com/setup.exe"}'
    assert canary.decode_body(gzip.compress(body.encode())) == body
    assert canary.decode_body(body.encode()) == body
    assert canary.decode_body(b"\x1f\x8bnot really gzip") != ""  # never raises
