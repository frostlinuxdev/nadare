# SPDX-License-Identifier: GPL-3.0-or-later
"""CI jobs must end on their own (2026-11-26: two test jobs sat 2-3 h after a crashed
xdist worker; one apt update failed on a mirror mid-sync)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = [".github/workflows/ci.yml", ".forgejo/workflows/ci.yml"]


def _jobs(rel: str) -> dict:
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))["jobs"]


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_every_job_has_a_time_limit(rel):
    for name, job in _jobs(rel).items():
        limit = job.get("timeout-minutes")
        assert isinstance(limit, int) and 0 < limit <= 60, (rel, name)


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_the_long_test_steps_have_their_own_limit(rel):
    steps = _jobs(rel)["test"]["steps"]
    for step in steps:
        run = step.get("run", "")
        if any(word in run for word in ("pytest", "apt-get", "uv sync")):
            assert isinstance(step.get("timeout-minutes"), int), (rel, step["name"])


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_apt_times_out_and_retries(rel):
    step = next(s for s in _jobs(rel)["test"]["steps"] if "apt-get" in s.get("run", ""))
    run = step["run"]
    assert "Acquire::http::Timeout=" in run, rel
    assert "Acquire::Retries=" in run, rel
    # A mirror mid-sync ("File has unexpected size") fails the whole update: try again.
    assert "for attempt in" in run, rel


# 2026-11-27: the apt step was a quarter of a test job (63-114 s; one 8 kB package stalled
# 76 s on the mirror) and its only flaky step. This week's downloaded packages are cached.
def _step(rel, prefix):
    return next(s for s in _jobs(rel)["test"]["steps"] if s.get("name", "").startswith(prefix))


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_the_qt_packages_come_from_a_weekly_cache(rel):
    steps = _jobs(rel)["test"]["steps"]
    cache = next(s for s in steps if s.get("uses", "").startswith("actions/cache@"))
    # Pinned to a full commit SHA like every other action.
    assert len(cache["uses"].split("@")[1].split()[0]) == 40, rel
    assert "steps.debs-week.outputs.week" in cache["with"]["key"], rel
    install = _step(rel, "Install Qt runtime libraries")
    assert steps.index(_step(rel, "Name this week")) < steps.index(cache) < steps.index(install)
    assert cache["with"]["path"] in install["run"].replace("$HOME", "~"), rel


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_the_install_tries_the_cache_first_and_falls_back_to_the_mirror(rel):
    run = _step(rel, "Install Qt runtime libraries")["run"]
    # An image that already has them (the self-hosted nadare-ci) skips the step entirely.
    assert run.index("dpkg -s $PKGS") < run.index("dpkg -i"), rel
    assert run.index("dpkg -i") < run.index("apt-get $APT update"), rel
    # The fallback downloads into the cached folder, so a miss fills the cache.
    assert "Dir::Cache::archives=" in run, rel
    # A cached set that installs but leaves a package missing still falls back.
    assert "dpkg -s $PKGS" in run, rel
