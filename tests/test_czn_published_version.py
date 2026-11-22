# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""CZN shows the published version from a cached value (#1480, W8-3 of #1310).

``status()`` never touches the network: ``check_update()`` (the background check) caches what
the update check saw, and the page names it next to the installed version.
"""

from __future__ import annotations

import json
import time

import pytest
from _czn_test_helpers import (  # noqa: F401 - pytest fixtures, found by name
    _install_game,
    _make_backend,
    _no_runner_downloads,
    _no_vcredist_network,
    _no_webview2_index,
    _reset_czn_process_state,
    make_backend_context,
)

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn import update as czn_update
from nadare.core.events import Cancelled


@pytest.fixture
def probes(monkeypatch):
    """Record every published-version lookup; answer with ``probes.latest``."""

    class Probe:
        latest = 42200
        calls: list[int] = []

    def resolve(installed):
        Probe.calls.append(installed)
        if isinstance(Probe.latest, BaseException):
            raise Probe.latest
        return Probe.latest, "index"

    Probe.calls = []
    monkeypatch.setattr(czn_update, "resolve_latest", resolve)
    return Probe


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("status() made a network call")

    monkeypatch.setattr(czn_update, "resolve_latest", refuse)
    monkeypatch.setattr(czn_update, "indexed_version", refuse)
    monkeypatch.setattr(czn_update, "latest_version", refuse)


def _backend(make_backend_context, tmp_path, version=42100):
    prefix = tmp_path / "prefix"
    _install_game(prefix, version)
    return _make_backend(make_backend_context, prefix)


def test_a_newer_published_version_is_shown_from_the_cache(make_backend_context, tmp_path, probes):
    backend = _backend(make_backend_context, tmp_path)
    backend.check_update()
    assert probes.calls == [42100]
    status = backend.status()
    assert status.data["update_available"] is True
    assert status.data["latest_version"] == "v42200"
    assert status.version == "v42100"


def test_status_reads_only_the_cache(make_backend_context, tmp_path, probes, monkeypatch):
    backend = _backend(make_backend_context, tmp_path)
    backend.check_update()

    def refuse(*_a, **_k):
        raise AssertionError("status() made a network call")

    monkeypatch.setattr(czn_update, "resolve_latest", refuse)
    monkeypatch.setattr(czn_update, "indexed_version", refuse)
    monkeypatch.setattr(czn_update, "latest_version", refuse)
    assert backend.status().data["latest_version"] == "v42200"


def test_with_no_cache_status_offers_nothing_and_stays_offline(
    make_backend_context, tmp_path, no_network
):
    status = _backend(make_backend_context, tmp_path).status()
    assert not status.data.get("update_available")
    assert "latest_version" not in status.data


@pytest.mark.parametrize("published", [42100, 42000])
def test_the_same_or_an_older_published_version_is_no_update(
    make_backend_context, tmp_path, probes, published
):
    probes.latest = published
    backend = _backend(make_backend_context, tmp_path)
    backend.check_update()
    status = backend.status()
    assert status.data["update_available"] is False
    assert "latest_version" not in status.data


def test_after_updating_the_old_cache_no_longer_offers_an_update(
    make_backend_context, tmp_path, probes
):
    backend = _backend(make_backend_context, tmp_path)
    backend.check_update()
    _install_game(backend.prefix(), 42200)  # the update ran
    assert backend.status().data["update_available"] is False


def test_a_fresh_cache_is_not_checked_again(make_backend_context, tmp_path, probes):
    backend = _backend(make_backend_context, tmp_path)
    backend.check_update()
    backend.check_update()
    assert probes.calls == [42100]


def test_a_stale_cache_is_checked_again(make_backend_context, tmp_path, probes):
    backend = _backend(make_backend_context, tmp_path)
    backend.check_update()
    cache = backend._published_cache_path()
    blob = json.loads(cache.read_text())
    blob["checked_at"] = time.time() - czn_backend.PUBLISHED_CHECK_INTERVAL - 5
    cache.write_text(json.dumps(blob))
    probes.latest = 42300
    backend.check_update()
    assert probes.calls == [42100, 42100]
    assert backend.status().data["latest_version"] == "v42300"


def test_a_failed_check_never_raises_and_keeps_the_old_value(
    make_backend_context, tmp_path, probes
):
    backend = _backend(make_backend_context, tmp_path)
    backend.check_update()
    cache = backend._published_cache_path()
    blob = json.loads(cache.read_text())
    blob["checked_at"] = 0
    cache.write_text(json.dumps(blob))
    probes.latest = OSError("offline")
    backend.check_update()  # must not raise
    assert backend.status().data["latest_version"] == "v42200"


def test_cancel_is_not_swallowed(make_backend_context, tmp_path, probes):
    probes.latest = Cancelled("stop")
    with pytest.raises(Cancelled):
        _backend(make_backend_context, tmp_path).check_update()


def test_not_installed_checks_nothing(make_backend_context, tmp_path, probes):
    backend = _make_backend(make_backend_context, tmp_path / "prefix")
    backend.check_update()
    assert probes.calls == []
    assert not backend.status().data.get("update_available")


def test_the_update_check_setting_off_checks_nothing(make_backend_context, tmp_path, probes):
    backend = _backend(make_backend_context, tmp_path)
    backend.ctx.config.data["check_updates"] = False
    backend.check_update()
    assert probes.calls == []


@pytest.mark.parametrize(
    "junk",
    ["not json", "[]", '{"version": "42200"}', '{"version": -1}', '{"version": true}'],
)
def test_a_damaged_cache_is_just_no_value(make_backend_context, tmp_path, no_network, junk):
    backend = _backend(make_backend_context, tmp_path)
    cache = backend._published_cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(junk)
    status = backend.status()
    assert not status.data.get("update_available")
    assert "latest_version" not in status.data


@pytest.mark.parametrize("junk", ['{"version": true}', '{"version": -1}', '{"version": 0}'])
def test_a_nonsense_cached_version_is_ignored_even_against_a_zero_install(
    make_backend_context, tmp_path, no_network, junk
):
    """``true`` (== 1) or a negative number must not pass as "newer" than a 0/-5 record."""
    backend = _backend(make_backend_context, tmp_path, version=-5)
    cache = backend._published_cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(junk)
    status = backend.status()
    assert not status.data.get("update_available")
    assert "latest_version" not in status.data
