# SPDX-License-Identifier: GPL-3.0-or-later
"""Index fetching: several base URLs, and the last good copy on disk when all of them fail."""

from __future__ import annotations

import json
import os
import time

import pytest

from nadare.core import hosting, indexfetch

A = "https://a.example/index/"
B = "https://b.example/index/"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("NADARE_INDEX_BASE", f"{A} {B}")


class Net:
    """Fake opener: ``routes`` maps url -> bytes, or an Exception to raise."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value


def doc(**kw) -> bytes:
    return json.dumps({"version": 1, **kw}).encode()


# ---- hosting: the list of bases -------------------------------------------------------


def test_index_bases_splits_on_whitespace_and_commas(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", f"{A},{B}\n https://c.example/x")
    assert hosting.index_bases() == [A, B, "https://c.example/x/"]


def test_index_bases_default_is_the_default_base(monkeypatch):
    monkeypatch.delenv("NADARE_INDEX_BASE", raising=False)
    assert hosting.index_bases() == hosting.DEFAULT_INDEX_BASE.split()
    assert hosting.index_bases()[-1] == "https://index.nadare.app/"


def test_index_bases_empty_disables(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    assert hosting.index_bases() == []


def test_index_base_stays_the_first_base():
    assert hosting.index_base() == A


# ---- fetching -------------------------------------------------------------------------


def test_first_base_is_used_and_cached():
    net = Net({A + "x/latest.json": doc(n=1)})
    got = indexfetch.fetch_json("x/latest.json", opener=net)
    assert got.data == {"version": 1, "n": 1}
    assert got.from_cache is False
    assert got.source == A + "x/latest.json"
    assert net.calls == [A + "x/latest.json"]
    assert indexfetch.cache_path(A, "x/latest.json").is_file()


def test_next_base_is_tried_when_the_first_fails():
    net = Net({A + "p.json": OSError("down"), B + "p.json": doc(n=2)})
    got = indexfetch.fetch_json("p.json", opener=net)
    assert got.data["n"] == 2 and got.source == B + "p.json"
    assert net.calls == [A + "p.json", B + "p.json"]


def test_invalid_json_counts_as_a_failure_and_the_next_base_is_tried():
    net = Net({A + "p.json": b"<html>captive portal</html>", B + "p.json": doc(n=3)})
    assert indexfetch.fetch_json("p.json", opener=net).data["n"] == 3


def test_validate_rejects_a_document_and_it_is_never_cached():
    net = Net({A + "p.json": doc(n=1), B + "p.json": doc(n=1)})

    def validate(data):
        return data.get("n") == 9

    with pytest.raises(indexfetch.IndexUnavailable):
        indexfetch.fetch_json("p.json", opener=net, validate=validate)
    assert not indexfetch.cache_path(A, "p.json").exists()


def test_all_bases_down_serves_the_cached_copy_marked_as_such():
    indexfetch.fetch_json("p.json", opener=Net({A + "p.json": doc(n=5)}))
    down = Net({A + "p.json": OSError("x"), B + "p.json": OSError("y")})
    got = indexfetch.fetch_json("p.json", opener=down)
    assert got.data["n"] == 5
    assert got.from_cache is True
    assert got.source == ""


def test_all_bases_down_and_no_cache_raises():
    down = Net({A + "p.json": OSError("x"), B + "p.json": OSError("y")})
    with pytest.raises(indexfetch.IndexUnavailable):
        indexfetch.fetch_json("p.json", opener=down)


def test_a_cached_copy_that_fails_validation_is_not_served():
    indexfetch.fetch_json("p.json", opener=Net({A + "p.json": doc(n=5)}))
    down = Net({A + "p.json": OSError("x"), B + "p.json": OSError("y")})
    with pytest.raises(indexfetch.IndexUnavailable):
        indexfetch.fetch_json("p.json", opener=down, validate=lambda d: d.get("n") == 6)


def test_a_corrupt_cache_file_is_ignored():
    target = indexfetch.cache_path(A, "p.json")
    target.parent.mkdir(parents=True)
    target.write_text("{not json")
    down = Net({A + "p.json": OSError("x"), B + "p.json": OSError("y")})
    with pytest.raises(indexfetch.IndexUnavailable):
        indexfetch.fetch_json("p.json", opener=down)


def test_disabled_index_does_not_fetch_and_does_not_use_the_cache(monkeypatch):
    indexfetch.fetch_json("p.json", opener=Net({A + "p.json": doc(n=5)}))
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    net = Net({})
    with pytest.raises(indexfetch.IndexUnavailable):
        indexfetch.fetch_json("p.json", opener=net)
    assert net.calls == []


def test_only_https_bases_are_used(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", f"http://plain.example/ {B}")
    net = Net({B + "p.json": doc(n=1)})
    indexfetch.fetch_json("p.json", opener=net)
    assert net.calls == [B + "p.json"]


@pytest.mark.parametrize("rel", ["../x.json", "/etc/passwd", "a/../../b.json", "", "a//b.json"])
def test_unsafe_relative_paths_are_refused(rel):
    net = Net({})
    with pytest.raises(ValueError):
        indexfetch.fetch_json(rel, opener=net)
    assert net.calls == []


def test_failed_fetch_leaves_the_previous_cache_untouched():
    indexfetch.fetch_json("p.json", opener=Net({A + "p.json": doc(n=5)}))
    indexfetch.fetch_json("p.json", opener=Net({A + "p.json": b"junk", B + "p.json": OSError("y")}))
    cached = json.loads(indexfetch.cache_path(A, "p.json").read_text())
    assert cached["n"] == 5


# ---- the default opener is the hardened one -------------------------------------------


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._data if n < 0 else self._data[:n]


def test_default_opener_only_follows_https_redirects_on_the_same_host(monkeypatch):
    seen = {}

    def _open(url, *, timeout, redirect_allowed):
        seen["allowed"] = redirect_allowed
        return _Resp(b"{}")

    monkeypatch.setattr(indexfetch.download, "http_open", _open)
    indexfetch.fetch_json("p.json")
    allowed = seen["allowed"]
    assert allowed(A + "other.json") is True
    assert allowed("http://a.example/index/p.json") is False
    assert allowed("https://evil.example/p.json") is False


def test_default_opener_refuses_a_document_over_64_kib(monkeypatch):
    big = b'{"x": "' + b"a" * 70000 + b'"}'
    monkeypatch.setattr(indexfetch.download, "http_open", lambda url, **kw: _Resp(big))
    with pytest.raises(indexfetch.IndexUnavailable):
        indexfetch.fetch_json("p.json")


# ---- scrutiny fixes ---------------------------------------------------------------------


def test_a_saved_copy_is_tied_to_the_base_that_served_it(monkeypatch):
    indexfetch.fetch_json("p.json", opener=Net({A + "p.json": doc(n=5)}))
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://other.example/")
    down = Net({"https://other.example/p.json": OSError("x")})
    with pytest.raises(indexfetch.IndexUnavailable):
        indexfetch.fetch_json("p.json", opener=down)


def test_the_first_base_with_a_saved_copy_is_used_when_all_are_down():
    served = Net({B + "p.json": doc(n=7), A + "p.json": OSError("x")})
    indexfetch.fetch_json("p.json", opener=served)
    down = Net({A + "p.json": OSError("x"), B + "p.json": OSError("y")})
    assert indexfetch.fetch_json("p.json", opener=down).data["n"] == 7


def test_a_saved_copy_older_than_the_age_limit_is_not_served():
    indexfetch.fetch_json("p.json", opener=Net({A + "p.json": doc(n=5)}))
    path = indexfetch.cache_path(A, "p.json")
    old = time.time() - (indexfetch.MAX_CACHE_AGE_SECONDS + 3600)
    os.utime(path, (old, old))
    down = Net({A + "p.json": OSError("x"), B + "p.json": OSError("y")})
    with pytest.raises(indexfetch.IndexUnavailable):
        indexfetch.fetch_json("p.json", opener=down)


def test_a_saved_copy_dated_far_in_the_future_is_not_served():
    """A wrong clock must not keep a saved copy alive forever."""
    indexfetch.fetch_json("p.json", opener=Net({A + "p.json": doc(n=5)}))
    path = indexfetch.cache_path(A, "p.json")
    future = time.time() + 400 * 24 * 3600
    os.utime(path, (future, future))
    down = Net({A + "p.json": OSError("x"), B + "p.json": OSError("y")})
    with pytest.raises(indexfetch.IndexUnavailable):
        indexfetch.fetch_json("p.json", opener=down)


def test_a_saved_copy_a_few_minutes_ahead_of_the_clock_is_still_served():
    """A small clock step back must not discard a copy that was just written."""
    indexfetch.fetch_json("p.json", opener=Net({A + "p.json": doc(n=5)}))
    path = indexfetch.cache_path(A, "p.json")
    ahead = time.time() + 300
    os.utime(path, (ahead, ahead))
    down = Net({A + "p.json": OSError("x"), B + "p.json": OSError("y")})
    assert indexfetch.fetch_json("p.json", opener=down).data["n"] == 5


def test_serving_a_saved_copy_is_logged_as_a_warning(caplog):
    indexfetch.fetch_json("p.json", opener=Net({A + "p.json": doc(n=5)}))
    down = Net({A + "p.json": OSError("x"), B + "p.json": OSError("y")})
    with caplog.at_level("WARNING"):
        indexfetch.fetch_json("p.json", opener=down)
    assert any("saved copy" in r.getMessage() for r in caplog.records)


def test_deeply_nested_json_is_a_failure_not_an_exception():
    nested = b"[" * 20000
    net = Net({A + "p.json": nested, B + "p.json": doc(n=9)})
    assert indexfetch.fetch_json("p.json", opener=net).data["n"] == 9


def test_a_validator_that_raises_is_a_failure():
    def boom(_data):
        raise RuntimeError("bad validator")

    net = Net({A + "p.json": doc(n=1), B + "p.json": doc(n=1)})
    with pytest.raises(indexfetch.IndexUnavailable):
        indexfetch.fetch_json("p.json", opener=net, validate=boom)
