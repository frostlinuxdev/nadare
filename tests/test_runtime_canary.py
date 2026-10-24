# SPDX-License-Identifier: GPL-3.0-or-later
"""The runtime canary warns when a runner needs a Steam Runtime the pinned umu lacks."""

from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

from nadare.core import umu

REPO = Path(__file__).resolve().parents[1]


def _canary():
    spec = importlib.util.spec_from_file_location(
        "runtime_canary", REPO / "scripts" / "check_runtime_canary.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_HEAD = '"manifest"\n{\n  "version" "2"\n  "commandline" "/proton %verb%"\n'
MANIFEST = _HEAD + '  "require_tool_appid" "4183110"\n}\n'
NO_REQUIRE = _HEAD + "}\n"


def test_parse_required_appid() -> None:
    canary = _canary()
    assert canary.parse_required_appid(MANIFEST) == 4183110
    assert canary.parse_required_appid(NO_REQUIRE) is None
    assert canary.parse_required_appid("") is None
    assert canary.parse_required_appid('"require_tool_appid" "abc"') is None


def _tar_xz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_manifest_is_found_in_a_streamed_tarball() -> None:
    canary = _canary()
    blob = _tar_xz(
        {
            "dwproton-11.0-15-x86_64/LICENSE": b"x" * 100,
            "dwproton-11.0-15-x86_64/files/lib/big.so": b"y" * 5000,
            "dwproton-11.0-15-x86_64/toolmanifest.vdf": MANIFEST.encode(),
        }
    )
    assert canary.read_manifest(io.BytesIO(blob)) == MANIFEST


def test_manifest_is_found_in_a_gzip_tarball_too() -> None:
    canary = _canary()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("GE-Proton11-11/toolmanifest.vdf")
        data = MANIFEST.encode()
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    assert canary.read_manifest(io.BytesIO(buf.getvalue())) == MANIFEST


def test_manifest_missing_gives_none() -> None:
    canary = _canary()
    blob = _tar_xz({"d/LICENSE": b"x", "d/files/deep/toolmanifest.vdf": b"nested"})
    assert canary.read_manifest(io.BytesIO(blob)) is None


def test_the_support_table_covers_the_runtimes_nadare_uses_today() -> None:
    assert 4183110 in umu.SUPPORTED_RUNTIME_APPIDS  # steamrt4
    assert 1628350 in umu.SUPPORTED_RUNTIME_APPIDS  # steamrt3 (sniper)


def test_check_flags_an_unknown_runtime_and_names_it() -> None:
    canary = _canary()
    state, problems = canary.check(
        {},
        {"dwproton": "dwproton-12.0-1"},
        lambda family, tag: 9999999,
        umu.SUPPORTED_RUNTIME_APPIDS,
    )
    assert state == {"dwproton": {"tag": "dwproton-12.0-1", "appid": 9999999}}
    assert len(problems) == 1
    assert "dwproton-12.0-1" in problems[0] and "9999999" in problems[0]
    assert umu.UMU_VERSION in problems[0]


def test_check_is_quiet_for_a_supported_or_absent_requirement() -> None:
    canary = _canary()
    _state, problems = canary.check(
        {},
        {"dwproton": "t1", "cachyos": "t2"},
        lambda family, tag: {"dwproton": 4183110, "cachyos": None}[family],
        umu.SUPPORTED_RUNTIME_APPIDS,
    )
    assert problems == []


def test_check_does_not_refetch_a_known_tag_but_still_reports_it() -> None:
    canary = _canary()
    calls: list[str] = []

    def fetch(family: str, tag: str):
        calls.append(tag)
        return 9999999

    state = {"dwproton": {"tag": "t1", "appid": 9999999}}
    supported = umu.SUPPORTED_RUNTIME_APPIDS
    new_state, problems = canary.check(state, {"dwproton": "t1"}, fetch, supported)
    assert calls == []  # cached, so no 300 MB download every run
    assert new_state == state
    assert len(problems) == 1  # the alert persists until umu catches up


def test_check_keeps_the_old_answer_when_a_download_fails() -> None:
    canary = _canary()

    def fetch(family: str, tag: str):
        raise OSError("offline")

    state = {"dwproton": {"tag": "old", "appid": 4183110}}
    supported = umu.SUPPORTED_RUNTIME_APPIDS
    new_state, problems = canary.check(state, {"dwproton": "new"}, fetch, supported)
    assert new_state == state
    assert problems == []


@pytest.mark.parametrize("text", ["", "{}", "[]", "not json"])
def test_load_state_tolerates_garbage(tmp_path: Path, text: str) -> None:
    canary = _canary()
    path = tmp_path / "state.json"
    path.write_text(text)
    assert canary.load_state(path) == {}
    assert canary.load_state(tmp_path / "missing.json") == {}
