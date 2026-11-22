# SPDX-License-Identifier: GPL-3.0-or-later
"""#805: the hold dialog, its per-hold override, and the Settings switch."""

from __future__ import annotations

import hashlib
import json

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QDialog
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from test_release_signature import sign

from nadare.core import holds, indexfetch, notices, releasesig, updatecheck
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.main_window_parts import banners
from nadare.gui.notice import HoldDialog
from nadare.gui.settings import SettingsPage

URL = "https://fixture.invalid/index/holds.json"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _item(**over):
    row = {
        "id": "nikke-2026-11",
        "kinds": ["launcher-nikke"],
        "below": None,
        "reason": "Reported compromise of the publisher's launcher.",
        "link": "https://github.com/frostlinuxdev/nadare/issues/805",
    }
    row.update(over)
    return holds.parse({"version": 1, "holds": [row]}, url=URL)[0]


def _signed_cache(tmp_path, monkeypatch) -> None:
    """Save a validly signed holds.json in the index cache, as a good start-up check would."""
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://fixture.invalid/index/")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    fingerprint = hashlib.sha1(public).digest()
    pinned = releasesig.PinnedKey(fingerprint.hex().upper(), public)
    verify = releasesig.verify_detached
    monkeypatch.setattr(releasesig, "verify_detached", lambda p, s: verify(p, s, key=pinned))
    body = json.dumps(
        {
            "version": 1,
            "serial": 1,
            "holds": [
                {
                    "id": "nikke-2026-11",
                    "kinds": ["launcher-nikke"],
                    "below": None,
                    "reason": "Reported compromise of the publisher's launcher.",
                    "link": "https://github.com/frostlinuxdev/nadare/issues/805",
                }
            ],
        }
    ).encode()
    envelope = {"body": body.decode(), "signature": sign(body, key, fingerprint)}
    cache = indexfetch.cache_path("https://fixture.invalid/index/", holds.CACHE_REL)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(json.dumps(envelope).encode())


def _window(tmp_path, monkeypatch, item):
    monkeypatch.setattr(updatecheck, "check_latest", lambda: None)
    monkeypatch.setattr(notices, "load", lambda *a, **k: [])
    monkeypatch.setattr(holds, "load", lambda *a, **k: [item])
    monkeypatch.setattr(holds, "known", lambda *a, **k: [item])
    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("g", backend="launcher-nikke", title="G")
    config.save()
    return MainWindow(Config.load(path)), path


def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_the_dialog_shows_the_reason_the_link_and_the_way_forward(qapp):
    item = _item()
    dialog = HoldDialog(item)
    try:
        assert item.reason in dialog.reason_label.text()
        assert item.link in dialog.link_label.text()
        assert dialog.start_button.text() == "I understand, start anyway"
        assert dialog.cancel_button.text() == "Cancel"
    finally:
        dialog.deleteLater()


def test_accepting_remembers_the_hold_id_for_this_profile(tmp_path, qapp, monkeypatch):
    item = _item()
    window, path = _window(tmp_path, monkeypatch, item)
    try:
        monkeypatch.setattr(
            HoldDialog, "exec", lambda self: QDialog.DialogCode.Accepted
        )
        assert window._confirm_hold("g") is True
        assert Config.load(path).profile("g")["hold_override"] == [item.id]
    finally:
        _close(window, qapp)


def test_a_new_hold_id_asks_again(tmp_path, qapp, monkeypatch):
    item = _item(id="new-hold")
    window, path = _window(tmp_path, monkeypatch, item)
    try:
        config = Config.load(path)
        config.profile("g")["hold_override"] = "old-hold"
        config.save()
        window.config = Config.load(path)
        seen = []

        def refuse(dialog):
            seen.append(dialog.hold.id)
            return QDialog.DialogCode.Rejected

        monkeypatch.setattr(HoldDialog, "exec", refuse)
        assert window._confirm_hold("g") is False
        assert seen == ["new-hold"]
    finally:
        _close(window, qapp)


def test_accepting_both_applicable_holds_allows_play(tmp_path, qapp, monkeypatch):
    """One backend can carry more than one hold; Play must ask for each and then start."""
    first, second = _item(id="first"), _item(id="second")
    window, path = _window(tmp_path, monkeypatch, first)
    monkeypatch.setattr(holds, "known", lambda *a, **k: [first, second])
    try:
        asked = []

        def accept(dialog):
            asked.append(dialog.hold.id)
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(HoldDialog, "exec", accept)
        assert window._confirm_hold("g") is True
        assert asked == ["first", "second"]
        saved = Config.load(path)
        assert (
            holds.blocking_hold(saved, "g", "launcher-nikke", holds=[first, second]) is None
        )
    finally:
        _close(window, qapp)


def test_the_switch_off_never_asks(tmp_path, qapp, monkeypatch):
    item = _item()
    window, path = _window(tmp_path, monkeypatch, item)
    try:
        window.config.data["check_holds"] = False

        def boom(_dialog):
            raise AssertionError("must not ask when the switch is off")

        monkeypatch.setattr(HoldDialog, "exec", boom)
        assert window._confirm_hold("g") is True
    finally:
        _close(window, qapp)


def test_play_asks_the_hold_before_it_starts(tmp_path, qapp, monkeypatch):
    item = _item()
    window, _path = _window(tmp_path, monkeypatch, item)
    try:
        asked = []
        monkeypatch.setattr(window, "_confirm_hold", lambda p: asked.append(p) or False)
        assert window._confirm_action("g", "launch") is False
        assert asked == ["g"]
    finally:
        _close(window, qapp)


def test_play_uses_only_the_known_cache_and_never_the_network(tmp_path, qapp, monkeypatch):
    _signed_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(holds, "_MEMO", None)
    monkeypatch.setattr(
        indexfetch,
        "fetch_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the GUI must not fetch")),
    )
    monkeypatch.setattr(banners.BannersMixin, "_start_holds_check", lambda self: None)
    monkeypatch.setattr(updatecheck, "check_latest", lambda: None)
    monkeypatch.setattr(notices, "load", lambda *a, **k: [])
    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("g", backend="launcher-nikke", title="G")
    config.save()
    window = MainWindow(Config.load(path))
    try:
        seen = []

        def accept(dialog):
            seen.append(dialog.hold.id)
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(HoldDialog, "exec", accept)
        assert window._confirm_hold("g") is True
        assert seen == ["nikke-2026-11"]
    finally:
        _close(window, qapp)


def test_settings_has_the_switch_on_by_default(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("g", backend="launcher-nikke", title="G")
    page = SettingsPage()
    try:
        page.load(config, "g")
        assert page.check_holds.isChecked()
        assert "dangerous" in page.check_holds.text().lower()
    finally:
        page.deleteLater()


def test_the_switch_saves_independently(tmp_path, qapp):
    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("g", backend="launcher-nikke", title="G")
    page = SettingsPage()
    try:
        page.load(config, "g")
        page.check_holds.setChecked(False)
        page.save()
        assert Config.load(path).data["check_holds"] is False
    finally:
        page.deleteLater()
