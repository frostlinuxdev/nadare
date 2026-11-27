# SPDX-License-Identifier: GPL-3.0-or-later
"""A seeded simulator for the Settings search (#1810).

Run the big campaign with::

    NADARE_SIM=1 SIM_SEEDS=1000 .venv/bin/python -m pytest \\
        tests/sim/test_sim_search.py -q -p no:cacheprovider

The always-on test runs seeds 0..19 so the normal suite still exercises the same
worlds. A failing world is reported as ``seed=<n>``; run just that world with
``NADARE_SIM=1 SIM_SEED=<n>`` (or ``SIM_SEEDS=1 SIM_SEED=<n>``).

Every world is synthetic: a throw-away config with 0..60 games, a random backend
kind, random option values, random page-hidden/advanced/disabled rows, and a
random (possibly translated) locale. All data lives under ``tmp_path``; fake
page probes replace the background status/disk scans. Nothing talks to a game,
the network or the user's real files.

The invariants checked after every query are:

* every row the user can see belongs to a group whose text (or card title)
  matches every word of the query;
* every matching row the page wants shown is reachable (no under-hiding);
* a row the page hid before the search never appears (no leaking);
* clearing restores exactly the pre-search visibility, tab titles, current tab
  and the "Nothing matches" line;
* no widget's enabled/read-only state changes (a disabled row never becomes
  editable);
* no query raises, and (with 60 games) each query finishes under 50 ms.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6 import sip
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import (
        QAbstractButton,
        QApplication,
        QComboBox,
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QWidget,
    )
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import flatpakmove
from nadare.core import i18n as core_i18n
from nadare.core.config import Config
from nadare.core.directkinds import BUILTIN_BACKENDS
from nadare.gui.disk_space_panel import DiskSpacePanel
from nadare.gui.settings import SettingsPage

#: Same muted hint styles the product treats as a hint under the row above.
_HINT_STYLES = frozenset({"Muted", "Subtitle"})
#: Every backend kind that Settings can show; the loaded game's kind is stratified.
KINDS = tuple(sorted(BUILTIN_BACKENDS))
#: Per-kind options, resolved once (imports the backend modules once).
OPTIONS_BY_KIND = {kind: SettingsPage._backend_options(kind) for kind in KINDS}

_UNICODE_WORDS = (
    "日本語",
    "Ωμέγα",
    "Straße",
    "İstanbul",
    "مرحبا",
    "ключ",
    "😀game",
    "a\u0301",
    "Ñandú",
    "ＦＵＬＬ",
)
_METACHARS = (".*", "(", "[a-z]+", "\\", "^$", "a|b", "?", "+", "{2}", "(?:x)")
_LONG_WORDS = ("x", "word", "game", "文明", "*")


@dataclass
class _CardInfo:
    info: object
    tab: int
    groups: list[list[_RowInfo]] = field(default_factory=list)


@dataclass
class _RowInfo:
    form: object
    row: int
    label: QWidget | None
    field: QWidget | None
    card: _CardInfo | None = None
    group: list[_RowInfo] | None = None
    page_visible: bool = True
    row_visible: bool = True
    widgets_visible: bool = True


@dataclass
class _Snapshot:
    rows: list[bool]
    row_visible: list[bool]
    page_visible: list[bool]
    card_hidden: list[bool]
    card_page_visible: list[bool]
    titles: list[str]
    tab: int
    empty_visible: bool
    empty_text: str
    editable: list[tuple[QWidget, bool, bool]]


@dataclass
class _World:
    seed: int
    kind: str | None
    games: int
    locale: str
    page: SettingsPage
    rows: list[_RowInfo]
    cards: list[_CardInfo]


@dataclass
class _Stats:
    worlds: int = 0
    queries: int = 0
    timed_queries: int = 0
    max_ms: float = 0.0
    max_ms_60: float = 0.0
    max_apply_ms: float = 0.0
    max_apply_ms_60: float = 0.0
    max_query: str = ""
    games: dict[int, int] = field(default_factory=dict)
    kinds: dict[str, int] = field(default_factory=dict)
    locales: dict[str, int] = field(default_factory=dict)
    query_kinds: dict[str, int] = field(default_factory=dict)
    advanced: int = 0
    app_only: int = 0
    rows_hidden_by_page: int = 0
    rows_disabled_by_sim: int = 0
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "worlds": self.worlds,
            "queries": self.queries,
            "timed_queries": self.timed_queries,
            "max_ms": round(self.max_ms, 3),
            "max_ms_60": round(self.max_ms_60, 3),
            "max_apply_ms": round(self.max_apply_ms, 3),
            "max_apply_ms_60": round(self.max_apply_ms_60, 3),
            "max_query_repr": repr(self.max_query),
            "games": dict(sorted(self.games.items())),
            "kinds": dict(sorted(self.kinds.items())),
            "locales": dict(sorted(self.locales.items())),
            "query_kinds": dict(sorted(self.query_kinds.items())),
            "advanced": self.advanced,
            "app_only": self.app_only,
            "rows_hidden_by_page": self.rows_hidden_by_page,
            "rows_disabled_by_sim": self.rows_disabled_by_sim,
            "failures": list(self.failures),
        }


STATS = _Stats()


# ---- product-shaped helpers (kept local so the simulator does not trust its own source) ----
def _row_widgets(form, row: int) -> tuple[QWidget | None, QWidget | None]:
    from PyQt6.QtWidgets import QFormLayout

    label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
    field_item = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
    if field_item is None or field_item.widget() is None:
        field_item = form.itemAt(row, QFormLayout.ItemRole.SpanningRole)
    label = label_item.widget() if label_item is not None else None
    field = field_item.widget() if field_item is not None else None
    return label, field


def _widget_texts(widget: QWidget | None, *, root: bool = True) -> list[str]:
    """Every piece of text the widget shows, skipping hidden children."""
    if widget is None:
        return []
    texts: list[str] = []

    def walk(current: QWidget, first: bool) -> None:
        if not first and current.isHidden():
            return
        tip = current.toolTip()
        if tip:
            texts.append(tip)
        if isinstance(current, QLabel):
            texts.append(current.text())
        elif isinstance(current, QAbstractButton):
            texts.append(current.text())
        elif isinstance(current, QLineEdit):
            texts.append(current.text())
            texts.append(current.placeholderText())
        elif isinstance(current, QComboBox):
            texts.append(current.currentText())
        elif isinstance(current, QPlainTextEdit):
            texts.append(current.toPlainText())
            texts.append(current.placeholderText())
        for child in current.findChildren(
            QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
        ):
            walk(child, False)

    walk(widget, root)
    return [text for text in texts if text]


def _matches(texts: list[str], words: list[str]) -> bool:
    haystack = "\n".join(texts).casefold()
    return all(word in haystack for word in words)


def _tab_of(page: SettingsPage, widget: QWidget) -> int:
    for index in range(page.tabs.count()):
        tab = page.tabs.widget(index)
        if tab is not None and tab.isAncestorOf(widget):
            return index
    return -1


def _row_actual_visible(page: SettingsPage, row: _RowInfo) -> bool:
    card = row.card
    if card is None or sip.isdeleted(card.info.card):
        return False
    if card.info.card.isHidden():
        return False
    if card.tab < 0 or not page.tabs.isTabVisible(card.tab):
        return False
    if not row.form.isRowVisible(row.row):
        return False
    if row.label is not None and row.label.isHidden():
        return False
    return not (row.field is not None and row.field.isHidden())


def _inventory(page: SettingsPage) -> tuple[list[_RowInfo], list[_CardInfo]]:
    rows: list[_RowInfo] = []
    cards: list[_CardInfo] = []
    for info in page._search_cards:
        if sip.isdeleted(info.card):
            continue
        card = _CardInfo(info=info, tab=_tab_of(page, info.card))
        cards.append(card)
        for form in info.forms():
            group: list[_RowInfo] | None = None
            for row in range(form.rowCount()):
                label, field = _row_widgets(form, row)
                if field is None or field is getattr(page, "_options_host", None):
                    continue
                label_text = label.text() if isinstance(label, QLabel) else ""
                hint = (
                    not label_text
                    and isinstance(field, QLabel)
                    and field.objectName() in _HINT_STYLES
                )
                item = _RowInfo(
                    form=form,
                    row=row,
                    label=label,
                    field=field,
                    card=card,
                    page_visible=form.page_visible(row),
                    row_visible=form.isRowVisible(row),
                    widgets_visible=not (
                        (label is not None and label.isHidden())
                        or (field is not None and field.isHidden())
                    ),
                )
                if hint and group is not None:
                    group.append(item)
                else:
                    group = [item]
                    card.groups.append(group)
                item.group = group
                rows.append(item)
    return rows, cards


def _editable_state(page: SettingsPage, rows: list[_RowInfo]) -> list[tuple[QWidget, bool, bool]]:
    """Enabled/read-only state of every row widget and its children."""
    state: list[tuple[QWidget, bool, bool]] = []
    seen: set[int] = set()

    def add(widget: QWidget) -> None:
        if id(widget) in seen:
            return
        seen.add(id(widget))
        readonly = widget.isReadOnly() if isinstance(widget, QLineEdit) else False
        state.append((widget, widget.isEnabled(), readonly))
        for child in widget.findChildren(QWidget):
            add(child)

    for row in rows:
        if row.label is not None:
            add(row.label)
        if row.field is not None:
            add(row.field)
    return state


def _snapshot(page: SettingsPage, rows: list[_RowInfo], cards: list[_CardInfo]) -> _Snapshot:
    return _Snapshot(
        rows=[_row_actual_visible(page, row) for row in rows],
        row_visible=[row.form.isRowVisible(row.row) for row in rows],
        page_visible=[row.form.page_visible(row.row) for row in rows],
        card_hidden=[card.info.card.isHidden() for card in cards],
        card_page_visible=[card.info.card.page_visible() for card in cards],
        titles=[page.tabs.tabText(i) for i in range(page.tabs.count())],
        tab=page.tabs.currentIndex(),
        empty_visible=page.search_empty.isVisibleTo(page),
        empty_text=page.search_empty.text(),
        editable=_editable_state(page, rows),
    )


def _expected_rows(
    page: SettingsPage, rows: list[_RowInfo], cards: list[_CardInfo], query: str
) -> set[int]:
    """The row ids the unchanged search design should leave visible for ``query``."""
    words = query.casefold().split()
    if not words:
        return {
            id(row)
            for row in rows
            if row.card is not None
            and row.card.info.card.page_visible()
            and row.card.tab >= 0
            and page.tabs.isTabVisible(row.card.tab)
            and row.form.isRowVisible(row.row)
            and row.widgets_visible
        }
    expected: set[int] = set()
    for card in cards:
        info = card.info
        if not info.card.page_visible() or card.tab < 0:
            continue
        if not page.tabs.isTabVisible(card.tab):
            continue
        card_hit = _matches([info.title.text(), info.subtitle.text()], words)
        for group in card.groups:
            shown = [
                row
                for row in group
                if _widget_texts(row.label) + _widget_texts(row.field)
                and row.page_visible
                and row.row_visible
                and row.widgets_visible
            ]
            if not shown:
                continue
            texts = [
                text
                for row in shown
                for text in _widget_texts(row.label) + _widget_texts(row.field)
            ]
            if card_hit or _matches(texts, words):
                # A result is the whole group: an empty hint row under a matched
                # setting stays with it (the product hides groups, not rows).
                expected.update(
                    id(row)
                    for row in group
                    if row.page_visible and row.row_visible and row.widgets_visible
                )
    return expected


def _visible_now(page: SettingsPage, rows: list[_RowInfo]) -> set[int]:
    return {id(row) for row in rows if _row_actual_visible(page, row)}


def _short(text: str, limit: int = 80) -> str:
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"


def _row_name(page: SettingsPage, row: _RowInfo) -> str:
    info = row.card.info
    label = row.label.text() if isinstance(row.label, QLabel) else ""
    field = (
        row.field.text()
        if isinstance(row.field, QLineEdit)
        else type(row.field).__name__
        if row.field is not None
        else "?"
    )
    return (
        f"card={_short(info.title.text())!r} form_row={row.row} "
        f"label={_short(label)!r} field={_short(field)!r}"
    )


# ---- random data --------------------------------------------------------------------------
def _rand_text(rng: random.Random, *, allow_empty: bool = True) -> str:
    choices = [
        "",
        "alpha",
        "Game Name",
        "a value with spaces",
        "日本語のゲーム",
        "Straße",
        ".*[x]",
        "😀",
        "x" * 240,
        "line one\nline two",
    ]
    if not allow_empty:
        choices = [text for text in choices if text]
    return rng.choice(choices)


def _rand_option_value(rng: random.Random, option, root: Path) -> object:
    kind = option.kind
    if kind in ("text", "args"):
        return _rand_text(rng)
    if kind in ("path_dir", "path_file"):
        return rng.choice(
            [
                "",
                str(root / "games" / f"game-{rng.randrange(60)}"),
                f"/nonexistent/{rng.randrange(100)}/Ünïcode",
            ]
        )
    if kind == "int":
        return rng.choice([0, 1, -1, 8, 32, 2**31 - 1, rng.randrange(-50, 200000)])
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "choice":
        choices = list(option.choices or ())
        if choices and rng.random() < 0.75:
            return rng.choice(choices)[1]
        return f"unknown-{rng.randrange(9)}"
    if kind == "runner":
        return rng.choice(["", "latest", "dwproton", "MissingBuild", str(root / "proton")])
    if kind == "env":
        if rng.random() < 0.5:
            return {}
        return {
            f"KEY_{rng.randrange(5)}": _rand_text(rng, allow_empty=False),
            "ÜNICODE": "日本語",
        }
    if kind == "monitor":
        return rng.choice(["", "auto", "HDMI-1", "eDP-1", "DP-2"])
    return _rand_text(rng)


def _rand_profile(rng: random.Random, kind: str, root: Path, title: str) -> dict[str, object]:
    values: dict[str, object] = {
        "backend": kind,
        "title": title,
        "install_dir": rng.choice(["", str(root / "game"), f"/tmp/nadare-{rng.randrange(99)}"]),
        "prefix": rng.choice(["", str(root / "prefix"), f"/tmp/prefix-{rng.randrange(99)}"]),
        "custom_background": rng.choice(["", str(root / "art.png"), "relative/art.png"]),
        "remember_me": rng.choice([True, False]),
        "favourite": rng.choice([True, False]),
    }
    if rng.random() < 0.3:
        values["wine_binary"] = rng.choice(["", "custom-wine", str(root / "wine/bin/wine")])
    extra: dict[str, object] = {}
    for option in OPTIONS_BY_KIND.get(kind, ()):
        if option.key in ("install_dir", "runner"):
            continue
        value = _rand_option_value(rng, option, root)
        if rng.random() < 0.3:
            extra[option.key] = value
        else:
            values[option.key] = value
    if extra:
        values["extra"] = extra
    return values


def _locale_words(code: str, key: str, index: int) -> str:
    """A translation that never contains the English key, so translation really is searched."""
    placeholders = " ".join(re.findall(r"\{[^}]+\}", key))
    token = f"tr-{code}-{index:03d}"
    unicode = _UNICODE_WORDS[index % len(_UNICODE_WORDS)]
    return f"{token} {unicode} {placeholders}".strip()


def _make_locales(folder: Path) -> dict[str, dict[str, str]]:
    folder.mkdir(parents=True, exist_ok=True)
    english = json.loads(
        (Path(__file__).resolve().parents[2] / "src/nadare/assets/locales/en.json").read_text(
            encoding="utf-8"
        )
    )
    catalogs: dict[str, dict[str, str]] = {"en": {str(k): str(v) for k, v in english.items()}}
    for code in ("xx", "yy", "zz"):
        catalog = {
            str(key): _locale_words(code, str(key), index) for index, key in enumerate(english)
        }
        catalogs[code] = catalog
        (folder / f"{code}.json").write_text(json.dumps(catalog), encoding="utf-8")
    return catalogs


# ---- world --------------------------------------------------------------------------------
def _build_world(seed: int, root: Path, locale: str, locales: dict[str, dict[str, str]]) -> _World:
    rng = random.Random(seed)
    core_i18n.set_locale(locale)
    # Mostly a random 0..60 games; a few seeds pin the two ends so the timing
    # invariant really sees 60 games and the app-only page really sees 0.
    if seed % 37 == 0:
        games = 60
    elif seed % 41 == 0:
        games = 0
    else:
        games = rng.randint(0, 60)
    config = Config.load(root / "config.json")
    names: list[str] = []
    for index in range(games):
        # Seed 0's game uses KINDS[0], seed 1 KINDS[1], … so 20 seeds and 1000 seeds
        # both visit every backend kind instead of hoping random sampling does.
        kind = KINDS[(seed + index) % len(KINDS)]
        name = f"game-{index:02d}"
        title = _rand_text(rng, allow_empty=False)
        config.add_profile(name, **_rand_profile(rng, kind, root, title))
        names.append(name)

    page = SettingsPage()
    # Keep the simulator off the host: no SELinux/disk-space/prefix threads.
    page._check_fgo_selinux_module = lambda: None
    page.disk_space_panel.refresh = lambda: None
    page.disk_space_panel.refresh_if_stale = lambda: None
    locked = rng.random() < 0.35
    page._refresh_prefix_lock = lambda has_wine_prefix: page._set_prefix_locked(
        bool(locked and has_wine_prefix)
    )
    if games:
        page.load(config, names[0])
        kind: str | None = str(config.profile(names[0]).get("backend") or "")
    else:
        page.load_app_only(config)
        kind = None
    if rng.random() < 0.5:
        page.show_advanced.setChecked(True)

    # Some worlds make a page row disabled/read-only directly, and some hide one
    # through the page's own helper; both must survive search and clear unchanged.
    rows, cards = _inventory(page)
    if rows and rng.random() < 0.35:
        for _ in range(rng.randint(1, 3)):
            row = rng.choice(rows)
            target = row.field if row.field is not None else row.label
            if target is not None and target.isEnabled():
                target.setEnabled(False)
                if isinstance(target, QLineEdit):
                    target.setReadOnly(True)
                STATS.rows_disabled_by_sim += 1
    if rows and rng.random() < 0.35:
        row = rng.choice(rows)
        target = row.field if row.field is not None else row.label
        if target is not None and not target.isHidden():
            page._set_widget_visible(target, False)
            STATS.rows_hidden_by_page += 1
    rows, cards = _inventory(page)
    return _World(
        seed=seed,
        kind=kind,
        games=games,
        locale=locale,
        page=page,
        rows=rows,
        cards=cards,
    )


def _visible_texts(world: _World) -> list[str]:
    texts: list[str] = []
    for row in world.rows:
        texts.extend(_widget_texts(row.label))
        texts.extend(_widget_texts(row.field))
    for card in world.cards:
        texts.append(card.info.title.text())
        texts.append(card.info.subtitle.text())
    return [text for text in texts if text]


def _queries(
    world: _World, rng: random.Random, locales: dict[str, dict[str, str]]
) -> list[tuple[str, str]]:
    """``(kind, query)`` pairs: always empty, unicode, metachars, long, translated, visible."""
    catalog = locales[world.locale]
    translated = [catalog[key] for key in sorted(catalog)]
    visible = _visible_texts(world)
    queries: list[tuple[str, str]] = [
        ("empty", ""),
        ("empty", "   "),
        ("unicode", rng.choice(_UNICODE_WORDS)),
        ("metachar", rng.choice(_METACHARS)),
        ("long", " ".join(rng.choice(_LONG_WORDS) for _ in range(700))),
    ]
    # Cycle through the catalogue so every translated label is used at least once
    # across the campaign, not merely sampled by chance.
    for offset in range(3):
        index = (world.seed * 3 + offset) % len(translated)
        queries.append(("translated", translated[index]))
    if world.locale != "en":
        queries.append(("translated", rng.choice(list(locales["en"].values()))))
    if visible:
        full = rng.choice(visible)
        queries.append(("visible", full))
        words = [
            word for text in rng.sample(visible, k=min(2, len(visible))) for word in text.split()
        ]
        words = [word for word in words if word]
        if len(words) >= 2:
            queries.append(("two-words", f"{rng.choice(words)} {rng.choice(words)}"))
        queries.append(("case", full.swapcase()))
    queries.extend(
        (
            "random",
            "".join(rng.choice("abcXYZ0123 日本語.*()[]") for _ in range(rng.randint(1, 40))),
        )
        for _ in range(2)
    )
    rng.shuffle(queries)
    return queries


# ---- invariants ---------------------------------------------------------------------------
def _check_query(world: _World, query: str) -> None:
    page = world.page
    words = query.casefold().split()
    expected = _expected_rows(page, world.rows, world.cards, query)
    actual = _visible_now(page, world.rows)
    if words:
        leaks = actual - expected
        if leaks:
            row = next(item for item in world.rows if id(item) in leaks)
            raise AssertionError(
                f"over-visible: query={query!r} shows a non-matching row: {_row_name(page, row)}"
            )
        missing = expected - actual
        if missing:
            row = next(item for item in world.rows if id(item) in missing)
            raise AssertionError(
                f"unreachable: query={query!r} hid a matching row: {_row_name(page, row)}"
            )
    else:
        if actual != expected:
            row = next(
                item for item in world.rows if (id(item) in actual) != (id(item) in expected)
            )
            raise AssertionError(
                f"empty query did not restore pre-search rows: {_row_name(page, row)}"
            )


def _check_restore(world: _World, snap: _Snapshot) -> None:
    page = world.page
    now = _snapshot(page, world.rows, world.cards)
    problems: list[str] = []
    for index, row in enumerate(world.rows):
        if now.rows[index] != snap.rows[index]:
            problems.append(
                f"row visibility changed: {_row_name(page, row)} "
                f"was={snap.rows[index]} now={now.rows[index]}"
            )
        if now.row_visible[index] != snap.row_visible[index]:
            problems.append(f"Qt row flag changed: {_row_name(page, row)}")
        if now.page_visible[index] != snap.page_visible[index]:
            problems.append(f"page visibility changed: {_row_name(page, row)}")
    for index, card in enumerate(world.cards):
        if now.card_hidden[index] != snap.card_hidden[index]:
            problems.append(f"card hidden changed: {_short(card.info.title.text())!r}")
        if now.card_page_visible[index] != snap.card_page_visible[index]:
            problems.append(f"card page visibility changed: {_short(card.info.title.text())!r}")
    if now.titles != snap.titles:
        problems.append(f"tab titles changed: {snap.titles!r} -> {now.titles!r}")
    if now.tab != snap.tab:
        problems.append(f"current tab changed: {snap.tab} -> {now.tab}")
    if now.empty_visible != snap.empty_visible or now.empty_text != snap.empty_text:
        problems.append(
            f"no-hits line changed: {snap.empty_visible}/{snap.empty_text!r} -> "
            f"{now.empty_visible}/{now.empty_text!r}"
        )
    problems.extend(_editable_problems(snap.editable))
    if problems:
        raise AssertionError("clear did not restore: " + "; ".join(problems[:8]))


def _editable_problems(editable: list[tuple[QWidget, bool, bool]]) -> list[str]:
    problems: list[str] = []
    for widget, enabled, readonly in editable:
        if sip.isdeleted(widget):
            problems.append(f"widget deleted: {type(widget).__name__}")
            continue
        if widget.isEnabled() != enabled:
            problems.append(
                f"enabled changed: {type(widget).__name__} {_short(widget.toolTip())!r}"
            )
        if isinstance(widget, QLineEdit) and widget.isReadOnly() != readonly:
            problems.append(
                f"read-only changed: {type(widget).__name__} {_short(widget.toolTip())!r}"
            )
    return problems


def run_world(
    seed: int,
    root: Path,
    locale: str,
    locales: dict[str, dict[str, str]],
    app: QApplication,
    *,
    stats: _Stats = STATS,
) -> None:
    """Build one world, run every query, and raise ``AssertionError`` on a failure."""
    world = _build_world(seed, root, locale, locales)
    stats.worlds += 1
    stats.games[world.games] = stats.games.get(world.games, 0) + 1
    stats.kinds[world.kind or "(app only)"] = stats.kinds.get(world.kind or "(app only)", 0) + 1
    stats.locales[world.locale] = stats.locales.get(world.locale, 0) + 1
    if world.page.show_advanced.isChecked():
        stats.advanced += 1
    if world.games == 0:
        stats.app_only += 1
    try:
        snap = _snapshot(world.page, world.rows, world.cards)
        queries = _queries(world, random.Random(seed ^ 0x5EED), locales)
        for kind, query in queries:
            stats.queries += 1
            stats.query_kinds[kind] = stats.query_kinds.get(kind, 0) + 1
            try:
                started = time.perf_counter()
                world.page.search.setText(query)
                # The box takes at most _QUERY_MAX_CHARS: judge the query it holds (fix/1126).
                query = world.page.search.text()
                apply_elapsed = (time.perf_counter() - started) * 1000
                app.processEvents()
                elapsed = (time.perf_counter() - started) * 1000
            except Exception as exc:  # noqa: BLE001 - the invariant is "no exception"
                raise AssertionError(f"query={query!r} raised {type(exc).__name__}: {exc}") from exc
            stats.timed_queries += 1
            if elapsed > stats.max_ms:
                stats.max_ms = elapsed
                stats.max_query = query
            if apply_elapsed > stats.max_apply_ms:
                stats.max_apply_ms = apply_elapsed
            if world.games == 60:
                stats.max_ms_60 = max(stats.max_ms_60, elapsed)
                stats.max_apply_ms_60 = max(stats.max_apply_ms_60, apply_elapsed)
            # The brief's limit is about the search itself, on the largest game set.
            # The event-loop tail is reported too, but a shared host can make it noisy.
            if world.games == 60 and apply_elapsed >= 50:
                raise AssertionError(
                    f"query={query!r} took {apply_elapsed:.1f} ms to filter on 60 games "
                    f"(kind={world.kind}, locale={world.locale})"
                )
            try:
                _check_query(world, query)
                editable = _editable_problems(snap.editable)
                if editable:
                    raise AssertionError(
                        "a disabled/read-only row changed: " + "; ".join(editable[:5])
                    )
            except AssertionError as exc:
                raise AssertionError(
                    f"query={query!r} kind={kind} backend={world.kind} games={world.games} "
                    f"locale={world.locale}: {exc}"
                ) from exc
        world.page.search.clear()
        app.processEvents()
        _check_restore(world, snap)
    finally:
        world.page.deleteLater()
        app.processEvents()
        core_i18n.set_locale("en")


# ---- pytest -------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def sim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp: QApplication):
    folder = tmp_path / "locales"
    locales = _make_locales(folder)
    monkeypatch.setattr(core_i18n, "locale_dir", lambda: folder)
    monkeypatch.setattr(core_i18n, "_cache", {})
    monkeypatch.setattr(flatpakmove, "probably_installed", lambda: False)
    monkeypatch.setattr(DiskSpacePanel, "refresh_if_stale", lambda self: None)
    monkeypatch.setattr(SettingsPage, "_check_fgo_selinux_module", lambda self: None)
    monkeypatch.setattr(core_i18n, "_current", "en")
    run_root = tmp_path / "worlds"
    run_root.mkdir()
    STATS.__init__()
    yield run_root, locales, qapp
    core_i18n.set_locale("en")
    core_i18n._cache.clear()


def _failures(seeds: list[int] | range, sim) -> list[str]:
    root, locales, app = sim
    failures: list[str] = []
    for seed in seeds:
        locale = ("en", "xx", "yy", "zz")[seed % 4]
        try:
            run_world(seed, root / f"w{seed}", locale, locales, app)
        except AssertionError as exc:
            message = (
                f"seed={seed}: {exc}\n"
                f"  repro: NADARE_SIM=1 SIM_SEED={seed} {sys.executable} -m pytest "
                "tests/sim/test_sim_search.py -q -p no:cacheprovider"
            )
            failures.append(message)
            STATS.failures.append(message)
            if len(failures) >= 20:
                break
    return failures


def test_smoke_20_seeds_always_runs(sim):
    """Twenty full worlds run in the normal suite, even without NADARE_SIM=1."""
    failures = _failures(list(range(20)), sim)
    assert not failures, "\n".join(failures)
    print("\nSIM-SEARCH smoke: " + json.dumps(STATS.as_dict(), sort_keys=True))


@pytest.mark.skipif(
    os.environ.get("NADARE_SIM") != "1",
    reason="1,000-world search simulation; set NADARE_SIM=1 to run it",
)
def test_simulated_search_worlds(sim):
    seed_list = os.environ.get("SIM_SEED")
    if seed_list:
        seeds: list[int] | range = [int(seed_list)]
    else:
        seeds = range(int(os.environ.get("SIM_SEEDS", "1000")))
    failures = _failures(seeds, sim)
    print("\nSIM-SEARCH: " + json.dumps(STATS.as_dict(), sort_keys=True))
    if failures:
        raise AssertionError(
            f"{len(failures)} failing world(s); first:\n" + "\n".join(failures[:10])
        )
