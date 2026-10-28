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


def _no_deep_check(monkeypatch: pytest.MonkeyPatch, canary) -> None:
    """main() also runs the Games.exe deep check, which downloads the real installer."""
    monkeypatch.setattr(canary, "check_endfield_games_exe", lambda *a, **k: [])


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
    _no_deep_check(monkeypatch, canary)
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
    _no_deep_check(monkeypatch, canary)
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
    _no_deep_check(monkeypatch, canary)
    head, _calls = fake_head(200, canary.nte_launcher.INSTALLER_SIZE)
    get_json, _jcalls = fake_json({"exe_url": "https://evil.example/setup.exe"})
    monkeypatch.setattr(canary, "_head", head)
    monkeypatch.setattr(canary, "_get_json", get_json)
    assert canary.main([]) == 4
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("CANARY:")]
    assert len(lines) == 1
    assert "Endfield" in lines[0]


def test_main_reports_a_canary_that_could_not_check_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = _canary()
    _no_deep_check(monkeypatch, canary)

    def head(url: str) -> tuple[int, int | None]:
        raise TimeoutError("offline")

    def get_json(url: str):
        raise TimeoutError("offline")

    monkeypatch.setattr(canary, "_head", head)
    monkeypatch.setattr(canary, "_get_json", get_json)
    assert canary.main([]) == 4  # blind is not fine: the heartbeat must go down
    captured = capsys.readouterr()
    assert "CANARY: installer pins: blind" in captured.out
    assert "installer pins" in captured.err


def test_a_gzip_compressed_api_body_is_decoded() -> None:
    canary = _canary()
    body = '{"exe_url": "https://hg-cdn.com/setup.exe"}'
    assert canary.decode_body(gzip.compress(body.encode())) == body
    assert canary.decode_body(body.encode()) == body
    assert canary.decode_body(b"\x1f\x8bnot really gzip") != ""  # never raises


# ------------------------------------------------ #527: does the installer still hold Games.exe?
URL = "https://launcher.hg-cdn.com/CNKz/GryphlineLauncher.exe"
DAY = 24 * 3600.0


class _Deep:
    """The injected pieces of the deep check, counting what they were asked to do."""

    def __init__(self, tmp_path, *, extract_error=None, download_error=None, have_7zip=True):
        self.state = tmp_path / "state" / "installer-canary.json"
        self.downloads: list[str] = []
        self.extracts = 0
        self.extract_error = extract_error
        self.download_error = download_error
        self.have_7zip = have_7zip

    def download(self, url, dest):
        self.downloads.append(url)
        if self.download_error:
            raise self.download_error
        Path(dest).write_bytes(b"installer")

    def extract(self, installer, dest):
        self.extracts += 1
        assert Path(installer).is_file()
        if self.extract_error:
            raise self.extract_error
        Path(dest).write_bytes(b"MZ")
        return dest

    def run(self, canary, now, data=None):
        get_json, _calls = fake_json({"exe_url": URL} if data is None else data)
        return canary.check_endfield_games_exe(
            get_json,
            self.download,
            self.extract,
            state_path=self.state,
            now=now,
            find_7zip=lambda: "/usr/bin/7z" if self.have_7zip else None,
        )


def test_an_installer_that_yields_games_exe_is_fine_and_is_not_rechecked_for_a_day(tmp_path):
    canary = _canary()
    deep = _Deep(tmp_path)
    assert deep.run(canary, 1000.0) == []
    assert deep.downloads == [URL] and deep.extracts == 1
    assert deep.run(canary, 1000.0 + DAY - 1) == []
    assert len(deep.downloads) == 1  # inside the day: no new 124 MiB download
    assert deep.run(canary, 1000.0 + DAY + 1) == []
    assert len(deep.downloads) == 2


def test_an_installer_without_games_exe_is_a_problem_that_names_the_step(tmp_path):
    canary = _canary()
    error = canary.endfield_shim.ShimError(
        "the launcher installer does not contain Games.exe", step="extract", hint="report"
    )
    deep = _Deep(tmp_path, extract_error=error)
    problems = deep.run(canary, 1000.0)
    assert len(problems) == 1
    assert "Games.exe" in problems[0] and "extract" in problems[0]


def test_a_known_problem_is_repeated_without_downloading_until_the_hour_is_up(tmp_path):
    canary = _canary()
    error = canary.endfield_shim.ShimError("no Games.exe", step="extract")
    deep = _Deep(tmp_path, extract_error=error)
    first = deep.run(canary, 1000.0)
    assert len(deep.downloads) == 1
    again = deep.run(canary, 1000.0 + 600)  # ten minutes later: same alarm, no download
    assert again == first and len(deep.downloads) == 1
    deep.extract_error = None  # the publisher fixed it
    assert deep.run(canary, 1000.0 + 3601) == []
    assert len(deep.downloads) == 2


def test_a_network_failure_is_only_a_note_and_retries_after_an_hour(tmp_path, capsys):
    canary = _canary()
    deep = _Deep(tmp_path, download_error=OSError("connection reset"))
    assert deep.run(canary, 1000.0) == []
    assert "Games.exe" in capsys.readouterr().err
    assert deep.run(canary, 1000.0 + 600) == [] and len(deep.downloads) == 1
    assert deep.run(canary, 1000.0 + 3601) == [] and len(deep.downloads) == 2


def test_without_7zip_the_deep_check_is_skipped_with_a_note(tmp_path, capsys):
    canary = _canary()
    deep = _Deep(tmp_path, have_7zip=False)
    assert deep.run(canary, 1000.0) == []
    assert deep.downloads == []  # never download 124 MiB it cannot open
    assert "7-Zip" in capsys.readouterr().err


@pytest.mark.parametrize("data", [{}, {"exe_url": None}, {"exe_url": "https://evil.example/x.exe"}])
def test_no_usable_installer_url_means_nothing_to_download(tmp_path, data):
    canary = _canary()
    deep = _Deep(tmp_path)
    assert deep.run(canary, 1000.0, data=data) == []  # check_endfield reports these
    assert deep.downloads == []


def test_a_corrupt_state_file_just_means_the_check_runs(tmp_path):
    canary = _canary()
    deep = _Deep(tmp_path)
    deep.state.parent.mkdir(parents=True)
    deep.state.write_text("{nope")
    assert deep.run(canary, 1000.0) == []
    assert len(deep.downloads) == 1


def test_main_runs_the_deep_check_and_reports_its_problem(tmp_path, monkeypatch, capsys):
    canary = _canary()
    monkeypatch.setattr(canary, "check_nte", lambda head: [])
    monkeypatch.setattr(canary, "check_endfield", lambda get_json: [])
    monkeypatch.setattr(canary, "check_endfield_games_exe", lambda *a, **k: ["Games.exe is gone"])
    assert canary.main(["--state", str(tmp_path / "s.json")]) == canary.PROBLEM
    assert "CANARY: Games.exe is gone" in capsys.readouterr().out
