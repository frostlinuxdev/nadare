# SPDX-License-Identifier: GPL-3.0-or-later
"""Every installer the index names must carry a pin before it is downloaded (#1593 item 2).

A publisher installer Nadare downloads and runs is either pinned by a block pin the index
publishes (NIKKE's miniloader, Endfield's launcher installer) or by a sha256 in this build
(the lepton-fgo tarball, NTE's launcher, the VC++ and WebView2 redistributables). A missing
or malformed pin is refused before a single byte is requested. A URL the user typed in the
profile is not an index entry and keeps working without one.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends import launcher
from nadare.backends.fgo import installer as fgo_installer
from nadare.backends.fgo import lepton
from nadare.core import (
    archivepins,
    download,
    endfield_shim,
    nte_launcher,
    storecatalog,
    vcredist,
    webview2,
)
from nadare.core import (
    backends as core_backends,
)
from nadare.core.backends import BackendError
from nadare.core.events import Cancel

URL = launcher.NikkeBackend.default_installer_url
DATA = bytes((i * 7 + 3) % 251 for i in range(20_000))
SHA = re.compile(r"[0-9a-f]{64}")


def serve_pins(monkeypatch, docs: dict[str, dict[str, archivepins.ArchivePin]]) -> list[str]:
    asked: list[str] = []

    def load(tag):
        asked.append(tag)
        return docs.get(tag, {})

    monkeypatch.setattr(archivepins, "load", load)
    return asked


@pytest.mark.parametrize("bad", [None, "malformed", "other-url", "invalid-blocks"])
def test_missing_or_malformed_index_pin_refuses_before_download(
    tmp_path, monkeypatch, bad
):
    def load(tag):
        if bad == "malformed":
            raise archivepins.PinError("bad checksum")
        if bad == "invalid-blocks":
            return {
                "installer": archivepins.ArchivePin(
                    4096, (archivepins.PinPart(URL, 2, (b"short",)),)
                )
            }
        if bad == "other-url":
            return {"installer": archivepins.pin_from_bytes(URL + "?new", b"MZ", 4096)}
        return {}

    monkeypatch.setattr(archivepins, "load", load)
    calls = []
    monkeypatch.setattr(download, "remote_size", lambda *a, **k: calls.append("HEAD"))
    monkeypatch.setattr(download, "download", lambda *a, **k: calls.append("GET"))
    backend = launcher.NikkeBackend(make_context(tmp_path, launcher.NikkeBackend.kind, {}))
    with pytest.raises(BackendError, match="index entry for.*NIKKE.*checksum"):
        backend._download_installer(URL)
    assert calls == []


def test_endfield_missing_pin_refuses_before_download(tmp_path, monkeypatch):
    monkeypatch.setattr(archivepins, "load", lambda tag: {})
    calls = []
    with pytest.raises(BackendError, match="index entry for.*Endfield.*no checksum"):
        endfield_shim._obtain_games(
            tmp_path / "pfx",
            tmp_path / "Games.exe",
            fetch_json=lambda url: {"exe_url": "https://launcher.hg-cdn.com/setup.exe"},
            download=lambda *a: calls.append("GET"),
            which=lambda _: "7z",
            extract_run=lambda *a, **k: calls.append("extract"),
        )
    assert calls == []


def test_custom_url_does_not_require_an_index_pin(tmp_path, monkeypatch):
    url = URL + "?custom"
    backend = launcher.NikkeBackend(
        make_context(tmp_path, launcher.NikkeBackend.kind, {"installer_url": url})
    )
    asked = serve_pins(monkeypatch, {})
    monkeypatch.setattr(download, "remote_size", lambda *a, **k: None)

    def fake_download(url, dest, **kw):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"MZ")
        return type("R", (), {"path": Path(dest)})()

    monkeypatch.setattr(download, "download", fake_download)
    assert backend._download_installer(url).read_bytes() == b"MZ"
    assert asked == []


@pytest.mark.parametrize("pin", [None, "bad"])
def test_vcredist_refuses_a_missing_or_malformed_pin_before_download(
    tmp_path, monkeypatch, pin
):
    monkeypatch.delenv("NADARE_VCREDIST_SHA256", raising=False)
    calls = []
    with pytest.raises(BackendError, match="index entry for Microsoft Visual C.*checksum"):
        vcredist.install(
            tmp_path / "prefix",
            profile={},
            cache_dir=tmp_path / "cache",
            run_in_prefix=lambda args: calls.append("run"),
            emit=lambda e: None,
            cancel=Cancel(),
            constant=pin,
            index_pins_fn=lambda: set(),
            index_source_fn=lambda: calls.append("source") or "https://x/setup.exe",
            download_fn=lambda *a: calls.append("download"),
        )
    assert calls == []


@pytest.mark.parametrize("pin,reason", [("", "no checksum"), ("bad", "malformed")])
def test_webview2_refuses_a_missing_or_malformed_pin_before_download(
    tmp_path, pin, reason
):
    calls = []
    with pytest.raises(BackendError, match="WebView2") as caught:
        webview2.ensure(
            tmp_path / "prefix",
            profile={},
            cache_dir_fn=lambda: calls.append("cache") or tmp_path / "cache",
            run_in_prefix=lambda args: calls.append("run"),
            emit=lambda e: None,
            cancel=Cancel(),
            constant=None,
            url=webview2.DEFAULT_URL,
            pin_fn=lambda: pin,
            index_pins_fn=lambda: set(),
            index_source_fn=lambda: calls.append("source") or "",
            overridden_fn=lambda: False,
            verify_fn=lambda path: None,
            download_fn=lambda *a: calls.append("download"),
            installer_environment=lambda: {},
            activity_label=lambda label: label,
            explain=lambda reason: reason,
        )
    assert reason in str(caught.value)
    assert calls == []


@pytest.mark.parametrize(
    "module,url",
    [
        (vcredist, "https://download.visualstudio.microsoft.com/x/vc_redist.x64.exe"),
        (
            webview2,
            "https://msedge.sf.dl.delivery.mp.microsoft.com/x/MicrosoftEdgeWebview2Setup.exe",
        ),
    ],
    ids=["vcredist", "webview2"],
)
@pytest.mark.parametrize("hashes", [None, [], ["bad"], ["ab" * 32, "bad"]])
def test_index_source_without_a_usable_checksum_is_not_used(
    monkeypatch, module, url, hashes
):
    monkeypatch.setattr(
        module,
        "_index_document",
        lambda: {"url": url, "sha256": hashes},
    )
    if hashes and SHA.fullmatch(hashes[0]):
        assert module.index_source() == url
    else:
        assert module.index_source() == ""


@pytest.mark.parametrize("sha", ["", "not-a-hash", "a" * 63, "g" * 64])
def test_nte_refuses_a_malformed_pin_before_download(tmp_path, monkeypatch, sha):
    monkeypatch.setattr(nte_launcher, "INSTALLER_SHA256", sha)
    calls = []
    with pytest.raises(nte_launcher.NteLauncherError, match="no valid checksum"):
        nte_launcher._fetch_installer(tmp_path / "NTE_setup.exe", lambda *a: calls.append(a))
    assert calls == []


@pytest.mark.parametrize("sha", ["", "not-a-hash", "a" * 63, "g" * 64])
def test_fgo_refuses_a_malformed_pin_before_download(tmp_path, sha):
    calls = []
    pin = fgo_installer.InstallerPin("1.0", "lepton-fgo-1.0.tar.zst", sha)
    adapter = lepton.LeptonFgo(prefix=tmp_path / "prefix", repo=None)
    with pytest.raises(lepton.LeptonError, match="no valid checksum"):
        fgo_installer.ensure_installer(adapter, pin=pin, fetch=lambda *a: calls.append(a))
    assert calls == []


def test_every_shipped_index_pinned_installer_refuses_without_its_pin(
    tmp_path, monkeypatch
):
    """Walk the built-in catalog: a shipped backend that can run an index installer must
    have a pin source, and the pin publisher must know how to publish it."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "publish_pins.py"
    spec = importlib.util.spec_from_file_location("publish_pins_catalog", script)
    publish = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish)

    index_tags = []
    for kind, title, *_ in storecatalog._catalog_rows():
        cls = core_backends.get("steam" if kind.startswith("steam-") else kind)
        if cls is None:
            continue
        tag = getattr(cls, "installer_pin_tag", None)
        if tag:
            index_tags.append(tag)
            # The runtime refuses before any request when the index has no pin...
            serve_pins(monkeypatch, {})
            calls = []
            monkeypatch.setattr(
                download, "remote_size", lambda *a, _c=calls, **k: _c.append("HEAD")
            )
            monkeypatch.setattr(
                download, "download", lambda *a, _c=calls, **k: _c.append("GET")
            )
            backend = cls(make_context(tmp_path / kind, kind, {}))
            with pytest.raises(BackendError, match="index entry for.*no checksum"):
                backend._download_installer(cls.default_installer_url)
            assert calls == []
            # ...and the node job has a resolver that can publish the pin it needs.
            publish.resolver_for(tag)
        elif (
            issubclass(cls, launcher.LauncherBackend)
            and cls.direct_mode is launcher.LauncherBackend.direct_mode
            and cls.default_installer_url
        ):
            pytest.fail(f"{title} can download and run an unpinned installer")

    index_tags.append(endfield_shim.INSTALLER_PIN_TAG)
    for tag in index_tags:
        publish.resolver_for(tag)
    assert "nikke-installer" in index_tags
    assert endfield_shim.INSTALLER_PIN_TAG in index_tags

    for digest in (
        fgo_installer.PIN.sha256,
        nte_launcher.INSTALLER_SHA256,
        vcredist.VCREDIST_SHA256,
        webview2.PINNED_SHA256,
    ):
        assert digest and SHA.fullmatch(digest.lower())


