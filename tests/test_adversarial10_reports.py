# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnostics stay useful on broken paths and obey explicit output bounds."""

from __future__ import annotations

import json

import pytest

from nadare.core import report
from nadare.core.config import Config


def build(tmp_path, profile, **kwargs):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "profiles": {"g": {"backend": "wine", "title": "Game", **profile}},
                "active_profile": "g",
            }
        )
    )
    config = Config.load(config_path)
    return report.build_report(
        config.data,
        logs_path=tmp_path / "logs",
        profiles_path=tmp_path / "profiles",
        username="synthetic-user",
        home=tmp_path / "home",
        hostname="synthetic-host",
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
        sys_root=tmp_path / "sys",
        os_release_path=tmp_path / "os-release",
        **kwargs,
    )


@pytest.mark.parametrize("field", ["install_dir", "prefix", "wineprefix"])
@pytest.mark.parametrize("value", ["bad\x00path", {"broken": "shape"}, ["broken"]])
def test_broken_profile_paths_do_not_stop_report(tmp_path, field, value):
    text = build(tmp_path, {field: value})
    assert text.startswith("# Nadare support report")
    assert "## Logs" in text


@pytest.mark.parametrize("limit", [64, 128, 256, 1024])
@pytest.mark.parametrize("warning", [False, True])
def test_report_respects_requested_byte_limit_with_and_without_warning(
    tmp_path, monkeypatch, limit, warning
):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "nadare.log").write_text("newest diagnostic line\n" * 300)
    monkeypatch.setattr(
        report,
        "_session_note",
        lambda *a: ["Warning: synthetic recovery warning."] if warning else [],
    )
    text = build(tmp_path, {}, max_bytes=limit)
    assert len(text.encode()) <= limit


@pytest.mark.parametrize(
    "field", ["password", "client_secret", "authorization", "cookies", "extra_env"]
)
def test_nested_sensitive_setting_values_are_omitted(tmp_path, field):
    marker = "fixture-private-value"
    text = build(tmp_path, {"extra": {"nested": {field: marker}, "safe": "visible"}})
    assert marker not in text and "visible" in text


def test_default_report_limit_is_respected_when_recovery_warning_is_present(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    body = ("diagnostic detail " * 125 + "\n") * 200
    for name in ["nadare.log", "g-launch.log"]:
        (logs / name).write_text(body)
    monkeypatch.setattr(
        report, "_session_note", lambda *a: ["Warning: synthetic recovery warning."]
    )
    text = build(tmp_path, {})
    assert len(text.encode()) <= report.MAX_REPORT_BYTES
