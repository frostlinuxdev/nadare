# SPDX-License-Identifier: GPL-3.0-or-later
"""Node failure reporting, using local command fakes only."""

import fcntl
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
PING = "https://hc.invalid/ping/fixture-key/check/?create=1&rid=abc#ignored"


@pytest.fixture
def node(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "pings"
    commands = {
        "curl": 'printf "%s\\n" "${@: -1}" >> "$PING_LOG"\n',
        "git": '''[[ "$1" == -C ]] || exit 99
shift 2
case "$*" in
  "pull --rebase --autostash --quiet") exit "${GIT_PULL_RC:-0}" ;;
  "add -- catalog/archive-pins"|"diff --cached --quiet -- catalog/archive-pins") exit 0 ;;
  "rev-parse HEAD") echo fixture-head ;;
  *) exit 99 ;;
esac
''',
        "publisher": 'echo "g: FAILED: fixture failure"\nexit "${PUBLISH_RC:-2}"\n',
    }
    for name, body in commands.items():
        path = bindir / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o700)
    index = tmp_path / "index"
    (index / ".git").mkdir(parents=True)

    def run(script="publish_pins_node.sh", heartbeat=PING, **changes):
        log.unlink(missing_ok=True)
        env = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "XDG_RUNTIME_DIR": str(tmp_path),
            "PING_LOG": str(log),
            "NADARE_INDEX_DIR": str(tmp_path / "missing"),
            "NADARE_PIN_INDEX_DIR": str(index),
            "NADARE_PIN_PUBLISHER": str(bindir / "publisher"),
            "NADARE_PIN_PUSH": "0",
            "NADARE_PIN_STATUS": str(tmp_path / "status.json"),
            "NADARE_PIN_GAMES": "g",
            **changes,
        }
        if heartbeat is not None:
            env["NADARE_HEARTBEAT_URL"] = heartbeat
            env["NADARE_PIN_HEARTBEAT_URL"] = heartbeat
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / script)], env=env,
            capture_output=True, text=True, timeout=15,
        )
        assert "fixture-key" not in result.stdout + result.stderr
        pings = log.read_text().splitlines() if log.exists() else []
        return result, pings

    return run


@pytest.mark.parametrize("script", ["refresh_index.sh", "publish_pins_node.sh"])
def test_healthchecks_failure_keeps_query_after_fail_path(node, script):
    for url in (PING, PING.split("#")[0], PING.split("?")[0]):
        result, pings = node(script, heartbeat=url)
        assert result.returncode != 0
        assert len(pings) == 1
        parsed = urlsplit(pings[0])
        assert parsed.path == "/ping/fixture-key/check/fail"
        assert parsed.query == urlsplit(url).query
        assert parsed.fragment == ""


@pytest.mark.parametrize("failure", ["missing-index", "symlink", "lock-open", "flock-error"])
def test_publisher_setup_failure_reports_once(node, tmp_path, failure):
    changes = {}
    lock = tmp_path / "nadare-publish-pins.lock"
    if failure == "missing-index":
        changes["NADARE_PIN_INDEX_DIR"] = str(tmp_path / "absent")
    elif failure == "symlink":
        lock.symlink_to(tmp_path / "target")
    elif failure == "lock-open":
        lock.mkdir()
    else:
        flock = tmp_path / "bin" / "flock"
        flock.write_text("#!/bin/bash\nexit 70\n")
        flock.chmod(0o700)
    result, pings = node(**changes)
    assert result.returncode != 0
    if failure == "lock-open":
        assert "could not open lock" in result.stderr
    assert len(pings) == 1
    assert urlsplit(pings[0]).path.endswith("/fail")


def test_held_publisher_lock_skips_without_ping(node, tmp_path):
    with (tmp_path / "nadare-publish-pins.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, pings = node()
    assert result.returncode == 0
    assert pings == []


@pytest.mark.parametrize("script", ["refresh_index.sh", "publish_pins_node.sh"])
def test_failure_without_heartbeat_still_exits_nonzero(node, script):
    result, pings = node(script, heartbeat=None)
    assert result.returncode != 0
    assert pings == []


def test_success_without_heartbeat_is_supported(node):
    result, pings = node(heartbeat=None, PUBLISH_RC="0")
    assert result.returncode == 0, result.stderr
    assert pings == []


def test_failed_pull_reports_once(node):
    result, pings = node(GIT_PULL_RC="1")
    assert result.returncode != 0
    assert "git pull failed" in result.stderr
    assert len(pings) == 1
    assert urlsplit(pings[0]).path.endswith("/fail")
