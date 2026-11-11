# SPDX-License-Identifier: GPL-3.0-or-later
"""#1158: a pin that disagrees with the publisher's file refuses; a missing pin warns.

Before: ``attach`` dropped a pin that did not match and the install streamed on with the
archive's own CRCs only, silently. Now, per archive:

- the pin names the same URL but the size or publisher md5 differs: the file at a known
  address changed, which is what pins exist to catch -> refuse, before anything is written;
- the pin names a different URL: it is for another version (the pin job runs every few
  hours and lags a game update) -> install, with a visible "not checked" note;
- the game has had pins on this machine but none could be read now (offline, expired,
  unusable) -> install, with the note;
- a game that never had pins -> as before, no note.
"""

from __future__ import annotations

import pytest

from nadare.core import archivepins, directdl, hosting, indexfetch

BS = 65536
DATA = bytes(range(256)) * 700
URL = "https://cdn.example/1.0/g.zip"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    archivepins.clear_cache()
    yield
    archivepins.clear_cache()


def pin(url=URL, data=DATA, md5=None):
    return archivepins.pin_from_bytes(url, data, BS, publisher_md5=md5)


SIZE = len(DATA)


def plan(url=URL, size=SIZE, md5=None, name="g"):
    source = directdl.RemoteFile("g.zip", url, size, md5=md5)
    return directdl.Plan("T", "1", archives=(directdl.Archive(name, source, stream=True),))


def loaded(pins, *, available=True, known=True):
    return archivepins.Loaded(pins=pins, available=available, known=known)


# ---- check: the decision per archive ---------------------------------------------------------


def test_a_matching_pin_is_attached_and_says_nothing():
    p = pin()
    checked, notes = archivepins.check(plan(), loaded({"g": p}))
    assert checked.archives[0].pin == p
    assert notes == []


def test_same_url_different_size_refuses():
    with pytest.raises(archivepins.PinMismatch, match="changed"):
        archivepins.check(plan(size=len(DATA) + 1), loaded({"g": pin()}))


def test_same_url_different_publisher_md5_refuses():
    with pytest.raises(archivepins.PinMismatch):
        archivepins.check(plan(md5="cd" * 16), loaded({"g": pin(md5="ab" * 16)}))


def test_the_refusal_names_what_was_expected_and_what_was_found():
    with pytest.raises(archivepins.PinMismatch) as info:
        archivepins.check(plan(size=len(DATA) + 1), loaded({"g": pin()}))
    text = str(info.value)
    assert str(len(DATA)) in text and str(len(DATA) + 1) in text


def test_a_pin_for_another_version_installs_with_a_note():
    checked, notes = archivepins.check(
        plan(url="https://cdn.example/1.1/g.zip", size=len(DATA) + 5), loaded({"g": pin()})
    )
    assert checked.archives[0].pin is None
    assert len(notes) == 1 and "not checked" in notes[0].lower()


def test_a_pinned_game_without_a_pin_for_this_archive_installs_with_a_note():
    checked, notes = archivepins.check(plan(name="voice-pack"), loaded({"g": pin()}))
    assert checked.archives[0].pin is None
    assert len(notes) == 1 and "not checked" in notes[0].lower()


def test_unreadable_pins_for_a_game_that_had_them_install_with_a_note():
    checked, notes = archivepins.check(plan(), loaded({}, available=False, known=True))
    assert checked.archives[0].pin is None
    assert len(notes) == 1 and "not checked" in notes[0].lower()


def test_a_game_that_never_had_pins_says_nothing():
    checked, notes = archivepins.check(plan(), loaded({}, available=False, known=False))
    assert checked.archives[0].pin is None
    assert notes == []


def test_one_note_per_install_not_per_archive():
    two = directdl.Plan(
        "T",
        "1",
        archives=(
            directdl.Archive("a", directdl.RemoteFile("a.zip", URL + "a", 1), stream=True),
            directdl.Archive("b", directdl.RemoteFile("b.zip", URL + "b", 1), stream=True),
        ),
    )
    _, notes = archivepins.check(two, loaded({}, available=False, known=True))
    assert len(notes) == 1


# ---- load_state: available / known -----------------------------------------------------------


def _serve(monkeypatch, doc):
    def fetch(rel, **kw):
        return indexfetch.IndexResult(doc, False, "m")

    monkeypatch.setattr(archivepins.indexfetch, "fetch_json", fetch)


def _bases(monkeypatch):
    monkeypatch.setattr(hosting, "index_bases", lambda: ["https://idx.example/"])


def test_a_fetched_document_is_available_and_known(monkeypatch):
    _bases(monkeypatch)
    doc = archivepins.document({"g": pin()})
    _serve(monkeypatch, doc)
    state = archivepins.load_state("gfl2")
    assert state.available and state.known and state.pins == {"g": pin()}


def test_unavailable_with_a_saved_copy_on_disk_is_known(monkeypatch):
    _bases(monkeypatch)
    saved = indexfetch.cache_path("https://idx.example/", "catalog/archive-pins/gfl2.json")
    saved.parent.mkdir(parents=True)
    saved.write_text("{}")  # even an old or bad copy says: this game has pins

    def down(rel, **kw):
        raise indexfetch.IndexUnavailable("down")

    monkeypatch.setattr(archivepins.indexfetch, "fetch_json", down)
    state = archivepins.load_state("gfl2")
    assert not state.available and state.known and state.pins == {}


def test_unavailable_without_any_saved_copy_is_unknown(monkeypatch):
    _bases(monkeypatch)

    def down(rel, **kw):
        raise indexfetch.IndexUnavailable("down")

    monkeypatch.setattr(archivepins.indexfetch, "fetch_json", down)
    state = archivepins.load_state("gfl2")
    assert not state.available and not state.known


def test_an_unusable_document_counts_as_known_but_unavailable(monkeypatch):
    _bases(monkeypatch)
    monkeypatch.setattr(
        archivepins.indexfetch,
        "fetch_json",
        lambda rel, **kw: indexfetch.IndexResult({"schema": 99}, False, "m"),
    )
    state = archivepins.load_state("gfl2")
    assert not state.available and state.known


def test_load_still_returns_just_the_pins(monkeypatch):
    _bases(monkeypatch)
    doc = archivepins.document({"g": pin()})
    _serve(monkeypatch, doc)
    assert archivepins.load("gfl2") == {"g": pin()}
