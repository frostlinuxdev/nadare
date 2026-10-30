# SPDX-License-Identifier: GPL-3.0-or-later
"""scripts/publish_pins_node.sh: the node wrapper around publish_pins.py.

The publisher, curl and the git remote are all local fakes, so nothing here touches a network.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "publish_pins_node.sh"
HEARTBEAT = "https://kuma.example/api/push/SECRET-TOKEN-123?status=up&msg=OK&ping="

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="permission checks do not apply to root")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_exe(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.index = tmp_path / "index"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.index)], check=True)
        _git(self.index, "config", "user.name", "Test")
        _git(self.index, "config", "user.email", "test@example.invalid")
        (self.index / "README.md").write_text("index\n")
        (self.index / "webview2").mkdir()
        (self.index / "webview2" / "installer.json").write_text("old\n")
        _git(self.index, "add", ".")
        _git(self.index, "commit", "-qm", "initial")
        _git(self.index, "push", "-q", "origin", "HEAD")
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.status = tmp_path / "status.json"
        self.bin = tmp_path / "bin"
        self.bin.mkdir(exist_ok=True)
        self.curl_log = tmp_path / "curl.log"
        _write_exe(
            self.bin / "curl",
            f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {self.curl_log}\n',
        )
        self.publisher = tmp_path / "publisher.sh"
        self.runs = tmp_path / "publisher.runs"
        self.set_publisher(0)

    def set_publisher(self, code: int, *, write: bool = True, extra: str = "") -> None:
        body = f'echo "$@" >> {self.runs}\n'
        if write:
            body += (
                'mkdir -p "$2/catalog/archive-pins" && '
                'echo \'{"schema":1,"archives":{}}\' > "$2/catalog/archive-pins/g.json"\n'
            )
        body += extra
        if code == 0:
            body += 'echo "g: published"\n'
        elif code == 3:
            body += 'echo "g: unchanged"\n'
        else:
            body += 'echo "g: FAILED: boom" \n'
        body += f"exit {code}\n"
        _write_exe(self.publisher, "#!/usr/bin/env bash\n" + body)

    def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.tmp),
            "NADARE_PIN_INDEX_DIR": str(self.index),
            "NADARE_PIN_PUBLISHER": str(self.publisher),
            "NADARE_PIN_STATUS": str(self.status),
            "NADARE_PIN_GAMES": "g",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            **extra,
        }
        return subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60, check=False
        )

    def status_doc(self) -> dict:
        return json.loads(self.status.read_text())

    def pings(self) -> list[str]:
        return self.curl_log.read_text().splitlines() if self.curl_log.exists() else []

    def remote_head(self) -> str:
        return subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_a_published_pin_is_committed_pushed_and_reported(env):
    result = env.run(NADARE_PIN_HEARTBEAT_URL=HEARTBEAT)

    assert result.returncode == 0, result.stderr
    assert _git(env.index, "log", "-1", "--format=%s") == "catalog: refresh archive pins"
    assert _git(env.index, "show", "--name-only", "--format=", "HEAD") == (
        "catalog/archive-pins/g.json"
    )
    assert env.remote_head() == _git(env.index, "rev-parse", "HEAD")
    doc = env.status_doc()
    assert doc["ok"] is True
    assert doc["result"] == "published"
    assert doc["games"] == ["g"]
    assert doc["commit"] == _git(env.index, "rev-parse", "HEAD")
    assert isinstance(doc["time"], int)
    assert len(env.pings()) == 1
    assert "status=down" not in env.pings()[0]


def test_nothing_to_publish_makes_no_commit_and_still_pings(env):
    env.set_publisher(3, write=False)
    before = _git(env.index, "rev-parse", "HEAD")

    result = env.run(NADARE_PIN_HEARTBEAT_URL=HEARTBEAT)

    assert result.returncode == 0, result.stderr
    assert _git(env.index, "rev-parse", "HEAD") == before
    assert env.status_doc()["ok"] is True
    assert env.status_doc()["result"] == "unchanged"
    assert len(env.pings()) == 1


def test_a_failing_publisher_commits_nothing_and_reports_down(env):
    env.set_publisher(1, write=False)
    before = _git(env.index, "rev-parse", "HEAD")

    result = env.run(NADARE_PIN_HEARTBEAT_URL=HEARTBEAT)

    assert result.returncode == 1
    assert _git(env.index, "rev-parse", "HEAD") == before
    doc = env.status_doc()
    assert doc["ok"] is False
    assert "boom" in doc["message"]
    assert len(env.pings()) == 1
    assert "status=down" in env.pings()[0]
    assert "boom" in env.pings()[0]


def test_pins_that_were_published_before_a_failure_are_still_committed(env):
    """publish_pins writes each game's file atomically, so what exists is complete and valid."""
    env.set_publisher(1, write=True)
    before = _git(env.index, "rev-parse", "HEAD")

    result = env.run(NADARE_PIN_HEARTBEAT_URL=HEARTBEAT)

    assert result.returncode == 1
    assert _git(env.index, "show", "--name-only", "--format=", "HEAD") == (
        "catalog/archive-pins/g.json"
    )
    assert env.remote_head() == _git(env.index, "rev-parse", "HEAD") != before
    assert env.status_doc()["ok"] is False
    assert "status=down" in env.pings()[-1]


def test_only_the_pin_directory_is_committed(env):
    env.set_publisher(
        0, extra='echo changed > "$2/webview2/installer.json"; echo stray > "$2/stray.txt"\n'
    )

    result = env.run()

    assert result.returncode == 0, result.stderr
    assert _git(env.index, "show", "--name-only", "--format=", "HEAD") == (
        "catalog/archive-pins/g.json"
    )
    assert "webview2/installer.json" in _git(env.index, "status", "--short")


def test_without_a_push_the_commit_stays_local(env):
    before = env.remote_head()

    result = env.run(NADARE_PIN_PUSH="0")

    assert result.returncode == 0, result.stderr
    assert _git(env.index, "rev-parse", "HEAD") != before
    assert env.remote_head() == before
    assert env.status_doc()["result"] == "committed"


def test_a_failed_push_is_reported_down_and_exits_nonzero(env):
    _git(env.index, "remote", "set-url", "origin", str(env.tmp / "missing.git"))

    result = env.run(NADARE_PIN_HEARTBEAT_URL=HEARTBEAT)

    assert result.returncode != 0
    assert env.status_doc()["ok"] is False
    assert "status=down" in env.pings()[-1]


def test_the_heartbeat_url_is_never_printed_or_stored(env):
    env.set_publisher(1, write=False)

    result = env.run(NADARE_PIN_HEARTBEAT_URL=HEARTBEAT)

    for text in (result.stdout, result.stderr, env.status.read_text()):
        assert "SECRET-TOKEN-123" not in text


def test_a_broken_heartbeat_does_not_fail_the_run(env):
    _write_exe(env.bin / "curl", "#!/usr/bin/env bash\nexit 22\n")

    result = env.run(NADARE_PIN_HEARTBEAT_URL=HEARTBEAT)

    assert result.returncode == 0, result.stderr
    assert env.status_doc()["ok"] is True


def test_a_second_run_while_the_first_holds_the_lock_is_skipped(env):
    lock = env.tmp / "nadare-publish-pins.lock"
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = env.run(NADARE_PIN_HEARTBEAT_URL=HEARTBEAT)

    assert result.returncode == 0
    assert "already running" in result.stdout
    assert not env.runs.exists()
    assert env.pings() == []


def test_a_missing_index_checkout_fails_loudly(env):
    result = env.run(NADARE_PIN_INDEX_DIR=str(env.tmp / "nope"))

    assert result.returncode == 1
    assert "no git checkout" in result.stderr


def test_the_games_and_deadline_reach_the_publisher(env):
    result = env.run(NADARE_PIN_GAMES="aethergazer mongil", NADARE_PIN_DEADLINE="123")

    assert result.returncode == 0, result.stderr
    args = env.runs.read_text().split()
    assert args.count("--game") == 2
    assert "aethergazer" in args and "mongil" in args
    assert args[args.index("--deadline") + 1] == "123"


def test_a_remote_that_moved_is_rebased_before_the_publisher_runs(env):
    other = env.tmp / "other"
    subprocess.run(["git", "clone", "-q", str(env.remote), str(other)], check=True)
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.invalid")
    (other / "other.txt").write_text("x\n")
    _git(other, "add", ".")
    _git(other, "commit", "-qm", "other")
    _git(other, "push", "-q", "origin", "HEAD")

    result = env.run()

    assert result.returncode == 0, result.stderr
    assert (env.index / "other.txt").exists()
    assert env.remote_head() == _git(env.index, "rev-parse", "HEAD")


UNIT_DIR = ROOT / "packaging" / "systemd"


def test_the_unit_runs_the_wrapper_with_its_own_checkout_and_an_env_file():
    unit = (UNIT_DIR / "nadare-pin-publisher.service").read_text()

    assert "scripts/publish_pins_node.sh" in unit
    assert "NADARE_PIN_INDEX_DIR=" in unit
    assert "EnvironmentFile=-/etc/nadare-pin-publisher.env" in unit
    assert "nadare-index-refresh" not in unit.replace("refresh_index.sh", "")
    assert "Type=oneshot" in unit
    assert "NoNewPrivileges=true" in unit


def test_the_timer_is_periodic_and_catches_up_after_downtime():
    timer = (UNIT_DIR / "nadare-pin-publisher.timer").read_text()

    assert "OnUnitActiveSec=" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_the_unit_budget_exceeds_the_publisher_deadline():
    import importlib.util

    spec = importlib.util.spec_from_file_location("pp_for_unit", ROOT / "scripts/publish_pins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    unit = (UNIT_DIR / "nadare-pin-publisher.service").read_text()
    minutes = int(
        next(line for line in unit.splitlines() if line.startswith("TimeoutStartSec="))
        .split("=")[1]
        .removesuffix("min")
    )

    assert minutes * 60 > module.DEFAULT_DEADLINE
