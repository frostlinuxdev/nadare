# SPDX-License-Identifier: GPL-3.0-or-later
"""The index refresher fails loudly on a broken node setup instead of silently doing less (#600)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "refresh_index.sh"
UNIT = ROOT / "packaging" / "systemd" / "nadare-index-refresh.service"

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="permission checks do not apply to root")


def _index(tmp_path: Path) -> Path:
    index = tmp_path / "index"
    index.mkdir()
    subprocess.run(["git", "-C", str(index), "init", "-q"], check=True)
    return index


def _run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    full = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "NADARE_INDEX_DIR": str(_index(tmp_path)),
        "NADARE_SKIP_CANARY": "1",
        **env,
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=full, timeout=60, check=False
    )


def test_the_unit_reads_the_environment_file_the_script_tells_you_to_create():
    unit = UNIT.read_text(encoding="utf-8")
    assert "EnvironmentFile=-/etc/nadare-index-refresh.env" in unit


def test_a_lock_that_cannot_be_opened_is_an_error_not_already_running(tmp_path):
    runtime = tmp_path / "rt"
    runtime.mkdir()
    lock = runtime / "nadare-refresh-index.lock"
    lock.write_text("")
    lock.chmod(0)
    result = _run(tmp_path, XDG_RUNTIME_DIR=str(runtime))
    assert result.returncode != 0
    assert "already running" not in result.stdout
    assert "lock" in result.stderr


def test_without_a_runtime_dir_the_lock_lives_in_a_private_directory(tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    _run(tmp_path, TMPDIR=str(tmp))
    private = tmp / f"nadare-refresh-{os.getuid()}"
    assert private.is_dir()
    assert (private.stat().st_mode & 0o777) == 0o700
    assert not (tmp / "nadare-refresh-index.lock").exists()


def test_a_symlinked_private_lock_directory_is_refused(tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp / f"nadare-refresh-{os.getuid()}").symlink_to(elsewhere)
    result = _run(tmp_path, TMPDIR=str(tmp))
    assert result.returncode != 0
    assert "lock" in result.stderr
    assert not any(elsewhere.iterdir())


def test_a_symlinked_yostar_env_is_refused_not_skipped(tmp_path):
    real = tmp_path / "real.env"
    real.write_text("SALT_X=1\n")
    real.chmod(0o600)
    link = tmp_path / "yostar.env"
    link.symlink_to(real)
    result = _run(tmp_path, NADARE_YOSTAR_ENV=str(link), XDG_RUNTIME_DIR=str(tmp_path))
    assert result.returncode != 0
    assert "symlink" in result.stderr


def test_the_refresher_runs_the_art_index_canary_unless_canaries_are_skipped():
    text = SCRIPT.read_text(encoding="utf-8")
    block_start = text.index("check_art_index.py")
    before = text[:block_start]
    assert (
        before.rstrip()
        .splitlines()[-2]
        .lstrip()
        .startswith('if [[ -z "${NADARE_SKIP_CANARY:-}" ]]')
    )
    assert "canary_down" in text[block_start : block_start + 700]
    assert (
        'exit "$' not in text[block_start : block_start + 700]
    )  # a canary never blocks publishing


# ---- #1209 P0-4: a failing node says so, with its name -----------------------------------------
HEARTBEAT = "https://kuma.example/api/push/SECRET-TOKEN-123?status=up&msg=OK&ping="


def _fake_curl(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "curl.log"
    curl = bin_dir / "curl"
    curl.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {log}\n')
    curl.chmod(0o755)
    return bin_dir, log


def test_a_failed_run_pings_down_with_the_node_name(tmp_path):
    """sw1 failed for 24 days unseen: the shared heartbeat only heard the nodes that worked."""
    import socket

    bin_dir, log = _fake_curl(tmp_path)
    full = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "NADARE_INDEX_DIR": str(tmp_path / "no-such-index"),  # fails at once
        "NADARE_SKIP_CANARY": "1",
        "NADARE_HEARTBEAT_URL": HEARTBEAT,
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=full,
                            timeout=60, check=False)
    assert result.returncode != 0
    pings = log.read_text().splitlines() if log.exists() else []
    assert len(pings) == 1
    assert "status=down" in pings[0]
    assert socket.gethostname().split(".")[0] in pings[0]
    assert "SECRET-TOKEN-123" not in result.stdout + result.stderr  # the URL is a secret


HEALTHCHECKS = "https://hc.example/ping/SECRET-TOKEN-123"


def _clone_with_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    index = tmp_path / "index"
    subprocess.run(["git", "clone", "-q", str(remote), str(index)], check=True)
    subprocess.run(["git", "-C", str(index), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(index), "config", "user.email", "test@example.invalid"], check=True
    )
    (index / "README.md").write_text("index\n")
    subprocess.run(["git", "-C", str(index), "add", "."], check=True)
    subprocess.run(["git", "-C", str(index), "commit", "-qm", "initial"], check=True)
    subprocess.run(["git", "-C", str(index), "push", "-q", "origin", "HEAD"], check=True)
    return index


def test_a_failed_run_posts_the_message_to_the_healthchecks_fail_url(tmp_path):
    """#1215: Healthchecks reads <url>/fail, not Kuma's ?status=down, and takes the text as body."""
    import socket

    bin_dir, log = _fake_curl(tmp_path)
    full = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "NADARE_INDEX_DIR": str(tmp_path / "no-such-index"),  # fails at once
        "NADARE_SKIP_CANARY": "1",
        "NADARE_HEARTBEAT_URL": HEALTHCHECKS,
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=full,
                            timeout=60, check=False)
    assert result.returncode != 0
    pings = log.read_text().splitlines() if log.exists() else []
    assert len(pings) == 1
    assert f"{HEALTHCHECKS}/fail" in pings[0]
    assert "--data-raw" in pings[0]
    assert "status=down" not in pings[0]
    assert socket.gethostname().split(".")[0] in pings[0]
    assert "SECRET-TOKEN-123" not in result.stdout + result.stderr  # the URL is a secret


def test_a_run_with_nothing_to_publish_pings_the_healthchecks_url_plainly(tmp_path):
    """Up = a plain GET of the URL itself; only the down ping gets a /fail suffix."""
    bin_dir, log = _fake_curl(tmp_path)
    (bin_dir / "python3").write_text("#!/usr/bin/env bash\nexit 3\n")
    (bin_dir / "python3").chmod(0o755)
    index = _clone_with_remote(tmp_path)
    full = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "XDG_RUNTIME_DIR": str(tmp_path),
        "NADARE_INDEX_DIR": str(index),
        "NADARE_SKIP_CANARY": "1",
        "NADARE_HEARTBEAT_URL": HEALTHCHECKS,
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=full,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    pings = log.read_text().splitlines() if log.exists() else []
    assert len(pings) == 1
    assert HEALTHCHECKS in pings[0]
    assert "/fail" not in pings[0]


def test_a_kuro_token_canary_posts_to_the_healthchecks_fail_url(tmp_path):
    """#1215: a canary warning is a "down" too, so it must reach <url>/fail, not read as up."""
    bin_dir, log = _fake_curl(tmp_path)
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        '  */check_kuro_tokens.py) echo "CANARY: kuro token refused"; exit 4 ;;\n'
        "  */check_*.py) exit 0 ;;\n"
        "esac\n"
        "exit 3\n"
    )
    (bin_dir / "python3").chmod(0o755)
    index = _clone_with_remote(tmp_path)
    full = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "XDG_RUNTIME_DIR": str(tmp_path),
        "NADARE_INDEX_DIR": str(index),
        "NADARE_HEARTBEAT_URL": HEALTHCHECKS,
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=full,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    pings = log.read_text().splitlines() if log.exists() else []
    assert len(pings) == 1
    assert f"{HEALTHCHECKS}/fail" in pings[0]
    assert "CANARY: kuro token refused" in pings[0]
    assert "status=down" not in pings[0]


def test_a_run_that_finds_another_run_going_pings_nothing(tmp_path):
    bin_dir, log = _fake_curl(tmp_path)
    runtime = tmp_path / "rt"
    runtime.mkdir()
    holder = subprocess.Popen(  # another run holds the lock
        ["flock", str(runtime / "nadare-refresh-index.lock"), "sleep", "5"]
    )
    try:
        import time

        time.sleep(0.3)
        result = _run(
            tmp_path,
            XDG_RUNTIME_DIR=str(runtime),
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            NADARE_HEARTBEAT_URL=HEARTBEAT,
        )
    finally:
        holder.kill()
    assert result.returncode == 0 and "already running" in result.stdout
    assert not log.exists()
