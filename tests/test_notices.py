# SPDX-License-Identifier: GPL-3.0-or-later
"""#526: a text-only notices feed from the public index (no network: fetch is injected)."""

from __future__ import annotations

import json

import pytest

from nadare.core import notices, paths

URL = "https://idx.example/index/notices.json"
HOUR = 3600.0


def _n(**over):
    base = {"id": "n1", "text": "Endfield installs are failing; a fix is coming.", "severity": "warn"}  # noqa: E501
    base.update(over)
    return base


def _doc(*items):
    return {"notices": list(items)}


# ------------------------------------------------------------------ parsing
def test_a_minimal_notice_is_accepted_with_defaults():
    [item] = notices.parse(_doc({"id": "a", "text": "hello"}), url=URL)
    assert item.id == "a" and item.text == "hello" and item.severity == "info"
    assert item.link is None and item.below is None and item.kinds == ()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "x",
        {"notices": "x"},
        {"notices": [None, 5, "s", {}]},
        {"notices": [{"id": "", "text": "t"}]},
        {"notices": [{"id": "a", "text": ""}]},
        {"notices": [{"id": "a", "text": "   "}]},
        {"notices": [{"id": "a b", "text": "t"}]},  # ids are plain tokens
        {"notices": [{"id": "x" * 65, "text": "t"}]},
        {"notices": [{"id": "a", "text": "t", "severity": "panic"}]},
        {"notices": [{"id": "a", "text": "t", "below": "not-a-version"}]},
        {"notices": [{"id": "a", "text": "t", "kinds": "direct-bd2"}]},
    ],
)
def test_malformed_input_yields_no_notices(bad):
    assert notices.parse(bad, url=URL) == []


def test_text_is_clipped_and_stripped_of_control_characters():
    [item] = notices.parse(_doc(_n(text="  hi\x00\x1b[31m there\n" + "x" * 1000)), url=URL)
    assert len(item.text) <= notices.MAX_TEXT
    assert "\x00" not in item.text and "\x1b" not in item.text and "\n" not in item.text


def test_at_most_ten_notices_and_duplicate_ids_collapse():
    many = [_n(id=f"n{i}") for i in range(30)] + [_n(id="n0", text="dup")]
    items = notices.parse(_doc(*many), url=URL)
    assert len(items) == notices.MAX_NOTICES
    assert len({i.id for i in items}) == len(items)


@pytest.mark.parametrize(
    "link",
    [
        "http://github.com/frostlinuxdev/nadare",
        "https://evil.example/x",
        "https://github.com.evil.example/x",
        "https://user:pw@github.com/x",
        "https://github.com:8443/x",
        "javascript:alert(1)",
        "//github.com/x",
        "",
        5,
    ],
)
def test_a_link_off_the_allowlist_is_dropped_but_the_notice_stays(link):
    [item] = notices.parse(_doc(_n(link=link)), url=URL)
    assert item.link is None and item.text


@pytest.mark.parametrize(
    "link",
    [
        "https://github.com/frostlinuxdev/nadare/issues/1",
        "https://frostlinuxdev.github.io/",
        "https://idx.example/page",  # the host the feed came from
    ],
)
def test_allowlisted_links_are_kept(link):
    [item] = notices.parse(_doc(_n(link=link)), url=URL)
    assert item.link == link


# ---------------------------------------------------------------- applicability
def test_below_targets_older_versions_only():
    item = notices.parse(_doc(_n(below="0.1.0b11")), url=URL)[0]
    assert notices.applies(item, "0.1.0b10", ())
    assert not notices.applies(item, "0.1.0b11", ())
    assert not notices.applies(item, "0.2.0", ())


def test_an_unparseable_running_version_never_matches_a_version_rule():
    item = notices.parse(_doc(_n(below="1.0")), url=URL)[0]
    assert not notices.applies(item, "weird", ())


def test_kinds_target_only_users_with_such_a_game():
    item = notices.parse(_doc(_n(kinds=["direct-endfield"])), url=URL)[0]
    assert notices.applies(item, "0.1.0", ("direct-endfield", "stove-czn"))
    assert not notices.applies(item, "0.1.0", ("stove-czn",))
    assert not notices.applies(item, "0.1.0", ())


def test_both_rules_must_hold_and_no_rules_means_everyone():
    both = notices.parse(_doc(_n(below="0.2.0", kinds=["a"])), url=URL)[0]
    assert notices.applies(both, "0.1.0", ("a",))
    assert not notices.applies(both, "0.1.0", ("b",))
    assert not notices.applies(both, "0.3.0", ("a",))
    everyone = notices.parse(_doc(_n()), url=URL)[0]
    assert notices.applies(everyone, "0.9.9", ())


def test_pick_returns_the_most_severe_undismissed_applicable_notice():
    items = notices.parse(
        _doc(
            _n(id="i", severity="info"),
            _n(id="w", severity="warn"),
            _n(id="b", severity="block"),
            _n(id="x", severity="block", kinds=["nope"]),
        ),
        url=URL,
    )
    assert notices.pick(items, "0.1.0", (), dismissed=()).id == "b"
    assert notices.pick(items, "0.1.0", (), dismissed=("b",)).id == "w"
    assert notices.pick(items, "0.1.0", (), dismissed=("b", "w")).id == "i"
    assert notices.pick(items, "0.1.0", (), dismissed=("b", "w", "i")) is None


# ------------------------------------------------------------------ fetch + cache
@pytest.fixture
def cache_home(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")
    return tmp_path / "cache"


class _Fetch:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, url, timeout):
        self.calls += 1
        value = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(value, Exception):
            raise value
        return value


def test_a_good_feed_is_cached_for_six_hours(cache_home):
    fetch = _Fetch(_doc(_n()))
    assert [i.id for i in notices.load(URL, fetch=fetch, now=1000.0)] == ["n1"]
    assert [i.id for i in notices.load(URL, fetch=fetch, now=1000.0 + 5 * HOUR)] == ["n1"]
    assert fetch.calls == 1
    notices.load(URL, fetch=fetch, now=1000.0 + 6 * HOUR + 1)
    assert fetch.calls == 2


def test_a_failure_is_silent_backs_off_and_keeps_the_last_good_feed(cache_home):
    fetch = _Fetch(_doc(_n()), OSError("down"))
    notices.load(URL, fetch=fetch, now=1000.0)
    stale = notices.load(URL, fetch=fetch, now=1000.0 + 7 * HOUR)
    assert [i.id for i in stale] == ["n1"] and fetch.calls == 2
    notices.load(URL, fetch=fetch, now=1000.0 + 7 * HOUR + 600)
    assert fetch.calls == 2  # inside the backoff


def test_a_first_failure_returns_nothing_without_raising(cache_home):
    assert notices.load(URL, fetch=_Fetch(OSError("down")), now=1.0) == []


def test_a_disabled_feed_url_does_not_touch_the_network(cache_home):
    fetch = _Fetch(_doc(_n()))
    assert notices.load("", fetch=fetch, now=1.0) == []
    assert notices.load("http://idx.example/notices.json", fetch=fetch, now=1.0) == []
    assert fetch.calls == 0


def test_a_corrupt_cache_is_ignored(cache_home):
    cache_home.mkdir(parents=True)
    (cache_home / "notices.json").write_text("{nope")
    assert [i.id for i in notices.load(URL, fetch=_Fetch(_doc(_n())), now=5.0)] == ["n1"]


def test_cached_data_is_revalidated_on_load(cache_home):
    cache_home.mkdir(parents=True)
    (cache_home / "notices.json").write_text(
        json.dumps(
            {
                "url": URL,
                "ok_at": 100.0,
                "failed_at": None,
                "payload": _doc(_n(link="https://evil.example/x", severity="panic")),
            }
        )
    )
    assert notices.load(URL, fetch=_Fetch(OSError("no")), now=200.0) == []


def test_the_feed_url_comes_from_the_index_base(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://idx.example/index")
    assert notices.feed_url() == "https://idx.example/index/notices.json"
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    assert notices.feed_url() == ""
