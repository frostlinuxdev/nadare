# SPDX-License-Identifier: GPL-3.0-or-later
"""#1601: the doctor command lives in ``nadare.doctor`` and is re-exported by app."""

from __future__ import annotations

from pathlib import Path

import pytest

import nadare.doctor as doctor
from nadare import app

MOVED = (
    "_disk_space_report",
    "_print_fgo_readiness",
    "_print_tls_diagnosis",
    "_print_optional_tools",
    "_print_sandbox_status",
    "_run_doctor",
    "_doctor_report",
    "_DOCTOR_CAPTURE_LOCK",
    "_doctor_text",
)


@pytest.mark.parametrize("name", MOVED)
def test_app_reexports_the_same_object(name: str) -> None:
    obj = getattr(doctor, name)
    assert getattr(app, name) is obj
    if callable(obj) and hasattr(obj, "__module__"):  # defined in doctor, not only re-exported
        assert obj.__module__ == "nadare.doctor"


def test_app_module_is_under_1300_lines() -> None:
    source = Path(app.__file__).read_text(encoding="utf-8")
    assert len(source.splitlines()) < 1300


def test_doctor_text_reads_run_doctor_from_the_doctor_module(monkeypatch) -> None:
    """Patching ``nadare.doctor._run_doctor`` must reach the moved caller."""
    calls = []

    def fake_run_doctor(config, args=None):
        calls.append(config)
        print("fake doctor")
        return 0

    monkeypatch.setattr(doctor, "_run_doctor", fake_run_doctor)
    config = type("C", (), {"data": {}})()
    assert doctor._doctor_text(config) == "fake doctor"
    assert calls == [config]


def test_run_doctor_reads_doctor_report_from_the_doctor_module(monkeypatch, capsys) -> None:
    """Patching ``nadare.doctor._doctor_report`` must reach ``_run_doctor``."""

    def failing_report(config, args, blockers):
        print("GPU/Vulkan: fake but useful")
        raise RuntimeError("vulkaninfo exploded")

    monkeypatch.setattr(doctor, "_doctor_report", failing_report)
    with pytest.raises(RuntimeError):
        doctor._run_doctor(object())
    assert "GPU/Vulkan: fake but useful" in capsys.readouterr().out

    text = doctor._doctor_text(object())
    assert "GPU/Vulkan: fake but useful" in text
    assert "unavailable: vulkaninfo exploded" in text
