# SPDX-License-Identifier: GPL-3.0-or-later
"""#1411: the Store page resolved the same art hosts ~1,000 times a minute.

``resolves_publicly`` asked the resolver on every candidate of every card, and a test run asked it
again for every test (cold cache each time). Pi-hole's per-client limit (1000/min) then refused
every name from that host for ~40 s. Two fixes:

* the real resolver's answer is remembered for a few minutes (failures for less), so one Store
  pass resolves each host once;
* no test reaches a real resolver: ``conftest`` answers local names and refuses the rest.
"""

from __future__ import annotations

import socket

import pytest

from nadare.core import artwork


def _counting(*addresses):
    calls = []

    def resolver(host, port):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses]

    return resolver, calls


@pytest.fixture
def real_resolver(monkeypatch):
    """Make a counting fake the module's *default* resolver, as the real one is in the app."""
    resolver, calls = _counting("93.184.216.34")
    monkeypatch.setattr(artwork, "_RESOLVER", resolver)
    monkeypatch.setattr(artwork, "_DEFAULT_RESOLVER", resolver)
    artwork._RESOLVED.clear()
    yield calls
    artwork._RESOLVED.clear()


def test_the_same_host_is_resolved_once_per_pass(real_resolver):
    for _ in range(20):
        assert artwork.resolves_publicly("cdn.cloudflare.steamstatic.com") is True
    assert real_resolver == ["cdn.cloudflare.steamstatic.com"]


def test_hosts_are_remembered_separately(real_resolver):
    artwork.resolves_publicly("a.example.net")
    artwork.resolves_publicly("b.example.net")
    artwork.resolves_publicly("A.example.net.")
    assert real_resolver == ["a.example.net", "b.example.net"]


def test_an_answer_expires(real_resolver, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(artwork, "_now", lambda: now[0])
    artwork.resolves_publicly("cdn.example.net")
    now[0] += artwork.RESOLVE_TTL_SECONDS + 1
    artwork.resolves_publicly("cdn.example.net")
    assert real_resolver == ["cdn.example.net", "cdn.example.net"]


def test_a_failure_is_remembered_for_less_time(monkeypatch):
    calls = []

    def failing(host, port):
        calls.append(host)
        raise socket.gaierror("no network")

    monkeypatch.setattr(artwork, "_RESOLVER", failing)
    monkeypatch.setattr(artwork, "_DEFAULT_RESOLVER", failing)
    artwork._RESOLVED.clear()
    now = [1000.0]
    monkeypatch.setattr(artwork, "_now", lambda: now[0])

    assert artwork.resolves_publicly("cdn.example.net") is False
    assert artwork.resolves_publicly("cdn.example.net") is False
    assert calls == ["cdn.example.net"]
    now[0] += artwork.RESOLVE_FAIL_TTL_SECONDS + 1
    artwork.resolves_publicly("cdn.example.net")
    assert len(calls) == 2
    assert artwork.RESOLVE_FAIL_TTL_SECONDS < artwork.RESOLVE_TTL_SECONDS
    artwork._RESOLVED.clear()


def test_a_local_answer_is_remembered_as_unsafe(monkeypatch):
    resolver, calls = _counting("127.0.0.1")
    monkeypatch.setattr(artwork, "_RESOLVER", resolver)
    monkeypatch.setattr(artwork, "_DEFAULT_RESOLVER", resolver)
    artwork._RESOLVED.clear()
    assert artwork.resolves_publicly("127.0.0.1.nip.io") is False
    assert artwork.resolves_publicly("127.0.0.1.nip.io") is False
    assert calls == ["127.0.0.1.nip.io"]
    artwork._RESOLVED.clear()


def test_an_explicit_resolver_is_never_cached(monkeypatch):
    """Callers (and tests) that pass their own resolver always get a fresh answer."""
    artwork._RESOLVED.clear()
    resolver, calls = _counting("8.8.8.8")
    artwork.resolves_publicly("x.example.net", resolver)
    artwork.resolves_publicly("x.example.net", resolver)
    assert calls == ["x.example.net", "x.example.net"]


def test_a_patched_module_resolver_is_not_served_from_an_old_answer(monkeypatch):
    """Tests swap ``_RESOLVER``; an answer remembered from another resolver must not leak."""
    good, _ = _counting("8.8.8.8")
    monkeypatch.setattr(artwork, "_RESOLVER", good)
    monkeypatch.setattr(artwork, "_DEFAULT_RESOLVER", good)
    artwork._RESOLVED.clear()
    assert artwork.resolves_publicly("swap.example.net") is True
    bad, _ = _counting("10.0.0.1")
    monkeypatch.setattr(artwork, "_RESOLVER", bad)
    assert artwork.resolves_publicly("swap.example.net") is False
    artwork._RESOLVED.clear()


# ------------------------------------------------------------------ tests stay off the network


def test_tests_never_reach_a_real_resolver():
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("cdn.cloudflare.steamstatic.com", 443)
    with pytest.raises(socket.gaierror):
        artwork._RESOLVER("store.steampowered.com", None)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "app.localhost"])
def test_local_names_still_resolve_in_tests(host):
    assert socket.getaddrinfo(host, 80)
