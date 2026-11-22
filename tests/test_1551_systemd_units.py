# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""packaging/systemd/*.service: the maintainer jobs run sandboxed (#1551 item 2).

Both units are templates installed as *system* units with ``User=`` set (see
docs/PACKAGING.md), so the hardening below is about the job, not about a package.
The paths are checked against what the scripts they run actually write.
"""

from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_UNIT_DIR = _ROOT / "packaging" / "systemd"
_UNITS = ("nadare-index-refresh.service", "nadare-pin-publisher.service")

# Every path each job writes, from the scripts it runs. Each must be under one of its
# unit's ReadWritePaths= entries. /tmp is deliberately absent: PrivateTmp= gives the
# job a private, writable /tmp (the refresh lock and the canaries' temp dirs live there).
_WRITES = {
    "nadare-index-refresh.service": (
        "%h/nadare-index",  # NADARE_INDEX_DIR: pull --rebase, commit, push
        "%h/.cache",  # check_installer_pins.py state, sevenzip.find()'s copy
        "%h/nadare-index-refresh",  # check_runtime_canary.py state
        "%h/.local/state",  # publish_index.py --catalog bookkeeping
    ),
    "nadare-pin-publisher.service": (
        "%h/nadare-pins/nadare-index",  # NADARE_PIN_INDEX_DIR: pull/commit/push, .locks/
        "%h/nadare-pins/status.json",  # NADARE_PIN_STATUS, via a temp file in its directory
    ),
}


def _service(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read(_UNIT_DIR / name, encoding="utf-8")
    return parser


def _read_write_paths(service: configparser.ConfigParser) -> list[str]:
    raw = service.get("Service", "ReadWritePaths", fallback="")
    return [entry.lstrip("-") for entry in raw.split()]


def _environments(name: str) -> dict[str, str]:
    # Environment= may be given more than once; systemd appends, configparser keeps only
    # the last assignment, so read the raw lines here.
    found: dict[str, str] = {}
    for line in (_UNIT_DIR / name).read_text(encoding="utf-8").splitlines():
        if line.startswith("Environment="):
            key, value = line[len("Environment=") :].split("=", 1)
            found[key] = value
    return found


def _covers(entry: str, path: str) -> bool:
    """True when ``path`` is ``entry`` or below it (a directory prefix, not a name prefix)."""
    entry, path = entry.rstrip("/"), path.rstrip("/")
    return path == entry or path.startswith(entry + "/")


@pytest.mark.parametrize("name", _UNITS)
def test_each_unit_sets_the_sandbox_keys(name: str) -> None:
    service = _service(name)
    assert service.get("Service", "NoNewPrivileges") == "true"
    assert service.get("Service", "PrivateTmp") == "true"
    assert service.get("Service", "ProtectSystem") == "strict"
    assert service.get("Service", "ProtectHome") == "read-only"
    assert _read_write_paths(service), f"{name}: a strict ProtectSystem= needs ReadWritePaths="


@pytest.mark.parametrize("name", _UNITS)
def test_every_path_the_job_writes_is_in_read_write_paths(name: str) -> None:
    writable = _read_write_paths(_service(name))
    missing = [path for path in _WRITES[name] if not any(_covers(e, path) for e in writable)]
    assert not missing, f"{name}: not writable under a strict ProtectSystem=: {missing}"


def test_the_index_checkout_matches_the_read_write_path_entry() -> None:
    # The environment variable and the sandbox entry are separate strings: keep them
    # together when one of them is edited.
    service = _service("nadare-index-refresh.service")
    index = _environments("nadare-index-refresh.service")["NADARE_INDEX_DIR"]
    assert any(_covers(entry, index) for entry in _read_write_paths(service))


def test_the_pin_status_directory_matches_its_read_write_path_entry() -> None:
    service = _service("nadare-pin-publisher.service")
    status = _environments("nadare-pin-publisher.service")["NADARE_PIN_STATUS"]
    assert any(_covers(entry, status) for entry in _read_write_paths(service))


def test_the_refresh_job_cannot_write_its_own_checkout() -> None:
    # refresh_index.sh runs from ~/nadare but only writes the separate index checkout;
    # ProtectHome=read-only is what keeps the scripts themselves immune to the job.
    service = _service("nadare-index-refresh.service")
    start = service.get("Service", "ExecStart")
    repo = start.rsplit("/scripts/", 1)[0]
    assert repo == "%h/nadare"
    assert not any(_covers(entry, repo) for entry in _read_write_paths(service))


@pytest.mark.parametrize("name", _UNITS)
def test_each_unit_documents_the_user_to_run_as(name: str) -> None:
    # They are system units (a user unit cannot read the root-owned env file), so the
    # site-specific account is a commented placeholder, not an invented name.
    lines = (_UNIT_DIR / name).read_text(encoding="utf-8").splitlines()
    commented = [line for line in lines if line.lstrip().startswith("#") and "User=" in line]
    assert commented, f"{name}: no commented User= line"


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_systemd_analyze_verify_accepts_both_units() -> None:
    paths = [str(_UNIT_DIR / name) for name in _UNITS]
    result = subprocess.run(
        ["systemd-analyze", "verify", *paths], capture_output=True, text=True, check=False
    )
    text = result.stdout + result.stderr
    # The templates point at %h checkouts that do not exist on the test machine; that is
    # the one expected message. Anything else (unknown/typo'd key, bad value) is a fail.
    unexpected = [
        line for line in text.splitlines() if line.strip() and "is not executable" not in line
    ]
    assert not unexpected, text
