# SPDX-License-Identifier: GPL-3.0-or-later
"""Published block pins for the installers Nadare downloads and runs (#787).

The Arknights setup, the NIKKE miniloader and the Endfield launcher installer are rolling,
publisher-hosted files with no published hash. An index node pins each one (trust on first
use) under ``catalog/archive-pins/<tag>.json``; the client refuses a download that does not
match a pin for its exact URL, and keeps the host check alone when there is none.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.backends import launcher
from nadare.backends import winegame as wg
from nadare.core import archivepins, installerpins
from nadare.core import endfield_shim as es
from nadare.core.backends import BackendError

AK_URL = "https://launcher-pkg-ark-en.yo-star.com/install_pkg/a-1.8.1-setup.exe"
NIKKE_URL = "https://nikke-en.com/nikkeminiloader.exe"
EF_URL = "https://launcher.hg-cdn.com/CNKz/GryphlineLauncher_v0.1.25.exe?tracking=X"
DATA = bytes((i * 7 + 3) % 251 for i in range(20_000))
BS = 4096
MZ = b"MZ"


def pin(url: str, data: bytes = DATA) -> archivepins.ArchivePin:
    return archivepins.pin_from_bytes(url, data, BS)


def serve_pins(monkeypatch, docs: dict[str, dict[str, archivepins.ArchivePin]]) -> list[str]:
    asked: list[str] = []

    def load(tag):
        asked.append(tag)
        return docs.get(tag, {})

    monkeypatch.setattr(archivepins, "load", load)
    return asked


# ---------------------------------------------------------------------------
# the module
# ---------------------------------------------------------------------------
def test_each_installer_has_its_own_pin_tag():
    assert ak.ArknightsBackend.installer_pin_tag is None
    assert launcher.NikkeBackend.installer_pin_tag == "nikke-installer"
    assert es.INSTALLER_PIN_TAG == "endfield-installer"
    assert wg.WineGameBackend.installer_pin_tag is None


def test_pin_for_returns_the_pin_only_for_its_exact_url(monkeypatch):
    serve_pins(monkeypatch, {"nikke-installer": {installerpins.ARCHIVE: pin(NIKKE_URL)}})
    assert installerpins.pin_for("nikke-installer", NIKKE_URL) == pin(NIKKE_URL)
    assert installerpins.pin_for("nikke-installer", NIKKE_URL + "?v=2") is None
    assert installerpins.pin_for("arknights-installer", NIKKE_URL) is None


def test_pin_for_never_raises(monkeypatch):
    def boom(tag):
        raise RuntimeError("index down")

    monkeypatch.setattr(archivepins, "load", boom)
    assert installerpins.pin_for("nikke-installer", NIKKE_URL) is None


def test_verify_file_accepts_the_pinned_bytes(tmp_path):
    path = tmp_path / "setup.exe"
    path.write_bytes(DATA)
    installerpins.verify_file(path, pin(AK_URL))


@pytest.mark.parametrize(
    "served",
    [
        DATA[:-1],  # shorter
        DATA + b"x",  # longer
        DATA[:5000] + b"EVIL" + DATA[5004:],  # same size, one block changed
    ],
)
def test_verify_file_refuses_anything_else(tmp_path, served):
    path = tmp_path / "setup.exe"
    path.write_bytes(served)
    with pytest.raises(installerpins.InstallerPinError):
        installerpins.verify_file(path, pin(AK_URL))


def test_check_without_a_pin_says_only_the_host_was_checked(tmp_path, monkeypatch):
    serve_pins(monkeypatch, {})
    path = tmp_path / "setup.exe"
    path.write_bytes(DATA)
    notes: list[str] = []
    installerpins.check("nikke-installer", NIKKE_URL, path, info=notes.append)
    assert len(notes) == 1 and "host" in notes[0]
    assert path.read_bytes() == DATA


def test_check_with_a_pin_refuses_and_removes_a_different_file(tmp_path, monkeypatch):
    serve_pins(monkeypatch, {"nikke-installer": {installerpins.ARCHIVE: pin(NIKKE_URL)}})
    path = tmp_path / "setup.exe"
    path.write_bytes(DATA[:5000] + b"EVIL" + DATA[5004:])
    with pytest.raises(installerpins.InstallerPinError):
        installerpins.check("nikke-installer", NIKKE_URL, path, info=lambda m: None)
    assert not path.exists()


# ---------------------------------------------------------------------------
# the Wine installer download (the NIKKE miniloader, when the direct launcher download fails)
# ---------------------------------------------------------------------------
def _nikke_backend(tmp_path, settings=None):
    return launcher.NikkeBackend(make_context(tmp_path, launcher.NikkeBackend.kind, settings or {}))


def _fake_download(monkeypatch, served: bytes):
    def fake_download(url, dest, **kwargs):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(served)
        return type("R", (), {"path": Path(dest)})()

    monkeypatch.setattr(wg.download, "download", fake_download)
    monkeypatch.setattr(wg.download, "remote_size", lambda url, **kw: None)


def test_a_pinned_miniloader_that_matches_is_used(tmp_path, monkeypatch):
    asked = serve_pins(monkeypatch, {"nikke-installer": {installerpins.ARCHIVE: pin(NIKKE_URL)}})
    _fake_download(monkeypatch, DATA)
    path = _nikke_backend(tmp_path)._download_installer(NIKKE_URL)
    assert path.read_bytes() == DATA
    assert asked == ["nikke-installer"]


def test_a_swapped_miniloader_is_refused_and_deleted(tmp_path, monkeypatch):
    serve_pins(monkeypatch, {"nikke-installer": {installerpins.ARCHIVE: pin(NIKKE_URL)}})
    _fake_download(monkeypatch, DATA[:5000] + b"EVIL" + DATA[5004:])
    backend = _nikke_backend(tmp_path)
    with pytest.raises(BackendError) as caught:
        backend._download_installer(NIKKE_URL)
    assert "does not match" in str(caught.value)
    assert not any(p.is_file() for p in backend.cache_dir().rglob("*"))


def test_a_user_pinned_sha256_wins_and_the_index_is_not_asked(tmp_path, monkeypatch):
    import hashlib

    asked = serve_pins(monkeypatch, {"nikke-installer": {installerpins.ARCHIVE: pin(NIKKE_URL)}})
    _fake_download(monkeypatch, b"other bytes")
    backend = _nikke_backend(
        tmp_path, {"installer_sha256": hashlib.sha256(b"other bytes").hexdigest()}
    )
    backend._download_installer(NIKKE_URL)
    assert asked == []


def test_an_unpinned_installer_still_downloads(tmp_path, monkeypatch):
    serve_pins(monkeypatch, {})
    _fake_download(monkeypatch, DATA)
    assert _nikke_backend(tmp_path)._download_installer(NIKKE_URL).read_bytes() == DATA


def test_arknights_has_no_installer_pin_since_it_installs_from_the_signed_manifest(
    tmp_path, monkeypatch
):
    """With the index on, Arknights installs file by file from the signed Yostar manifest;
    the setup.exe path only runs with the index off, where no pin could be read anyway."""
    asked = serve_pins(monkeypatch, {})
    _fake_download(monkeypatch, DATA)
    backend = ak.ArknightsBackend(make_context(tmp_path, ak.KIND, {"installer_url": AK_URL}))
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://index.example/")
    assert backend.direct_mode()
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    assert not backend.direct_mode()
    backend._download_installer(backend.require_installer_url())
    assert asked == []


def test_a_launcher_game_without_a_pin_tag_never_asks_the_index(tmp_path, monkeypatch):
    asked = serve_pins(monkeypatch, {})
    _fake_download(monkeypatch, DATA)
    from _backend_helpers import fake_launcher_backend

    launcher_cls = fake_launcher_backend()
    backend = launcher_cls(make_context(tmp_path, launcher_cls.kind, {}))
    backend._download_installer(launcher_cls.default_installer_url)
    assert asked == []


# ---------------------------------------------------------------------------
# Endfield: Games.exe comes out of the launcher installer
# ---------------------------------------------------------------------------
def _obtain(tmp_path, served: bytes, pin_check):
    prefix = tmp_path / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    extracted: list = []

    def fake_extract(installer, dest, **kw):
        extracted.append(Path(installer).read_bytes())
        Path(dest).write_bytes(MZ + b"games")
        return dest

    def download(url, dest):
        Path(dest).write_bytes(served)

    kwargs = {} if pin_check is None else {"pin_check": pin_check}
    original = es.extract_games_exe
    es.extract_games_exe = fake_extract
    try:
        cache = es._obtain_games(
            prefix,
            tmp_path / "data" / "Games.exe",
            fetch_json=lambda _url: {"exe_url": EF_URL},
            download=download,
            which=lambda c: "/x/7z",
            extract_run=lambda *a, **k: None,
            **kwargs,
        )
    finally:
        es.extract_games_exe = original
    return cache, extracted


def test_endfield_checks_the_installer_against_its_pin_before_extracting(tmp_path, monkeypatch):
    serve_pins(monkeypatch, {"endfield-installer": {installerpins.ARCHIVE: pin(EF_URL)}})
    cache, extracted = _obtain(tmp_path, DATA, None)
    assert cache.read_bytes() == MZ + b"games" and extracted == [DATA]


def test_endfield_refuses_a_swapped_installer_at_the_fetch_step(tmp_path, monkeypatch):
    serve_pins(monkeypatch, {"endfield-installer": {installerpins.ARCHIVE: pin(EF_URL)}})
    with pytest.raises(es.ShimError) as caught:
        _obtain(tmp_path, DATA[:5000] + b"EVIL" + DATA[5004:], None)
    assert caught.value.step == "fetch"
    assert not (tmp_path / "data" / "Games.exe").exists()


def test_endfield_without_a_pin_extracts_as_before(tmp_path, monkeypatch):
    serve_pins(monkeypatch, {})
    cache, extracted = _obtain(tmp_path, DATA, None)
    assert extracted == [DATA]


# ---------------------------------------------------------------------------
# the node side: resolvers, trust on first use, re-pinning
# ---------------------------------------------------------------------------
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_pins.py"


@pytest.fixture(scope="module")
def pp():
    spec = importlib.util.spec_from_file_location("publish_pins_installers", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTALLER_TAGS = ("nikke-installer", "endfield-installer")


@pytest.mark.parametrize("tag", (*INSTALLER_TAGS, "bd2"))
def test_the_node_knows_the_new_tags_and_pins_them_on_first_use(pp, tag):
    assert callable(pp.resolver_for(tag))
    assert tag in pp.FIRST_USE_GAMES


def test_the_node_job_pins_them_by_default():
    script = (SCRIPT.parent / "publish_pins_node.sh").read_text(encoding="utf-8")
    line = next(x for x in script.splitlines() if x.startswith('games="${NADARE_PIN_GAMES:-'))
    defaults = line.split(":-", 1)[1].split("}", 1)[0].split()
    for tag in (*INSTALLER_TAGS, "bd2"):
        assert tag in defaults, tag


def test_the_node_does_not_pin_the_arknights_setup(pp):
    with pytest.raises(pp.PublishPinError):
        pp.resolver_for("arknights-installer")


def test_the_endfield_resolver_uses_the_api_link_and_its_size():
    plan = installerpins.resolve(
        "endfield-installer",
        fetch_json=lambda url: {"exe_url": EF_URL},
        head=lambda url: 129_568_544,
    )
    (archive,) = plan.archives
    assert archive.name == installerpins.ARCHIVE
    assert archive.source.url == EF_URL and archive.source.size == 129_568_544


def test_the_endfield_resolver_refuses_a_host_the_client_would_refuse():
    with pytest.raises(installerpins.InstallerPinError):
        installerpins.resolve(
            "endfield-installer",
            fetch_json=lambda url: {"exe_url": "https://evil.example/x.exe"},
            head=lambda url: 1,
        )


def test_the_nikke_resolver_pins_the_fixed_miniloader_url():
    plan = installerpins.resolve("nikke-installer", fetch_json=lambda url: 1 / 0, head=lambda u: 9)
    assert plan.archives[0].source.url == launcher.NikkeBackend.default_installer_url


class Stream:
    def __init__(self, data: bytes = DATA):
        self.data, self.opened = data, 0

    def __call__(self, url, headers):
        self.opened += 1
        yield self.data


def _installer_plan(url: str, size: int | None):
    return installerpins.installer_plan("x-installer", url, size)


def test_a_versioned_installer_is_not_downloaded_again_while_url_and_size_hold(pp, tmp_path):
    stream = Stream()
    resolve = lambda: _installer_plan(EF_URL, len(DATA))  # noqa: E731
    pp.publish_pin(
        tmp_path, "endfield-installer", resolve=resolve, open_stream=stream, block_size=BS
    )
    again = pp.publish_pin(
        tmp_path, "endfield-installer", resolve=resolve, open_stream=stream, block_size=BS
    )
    assert again == "unchanged" and stream.opened == 1


def test_the_fixed_url_miniloader_is_hashed_again_on_every_run(pp, tmp_path):
    """Its URL never changes, so a same-size replacement would otherwise keep a stale pin."""
    resolve = lambda: _installer_plan(NIKKE_URL, len(DATA))  # noqa: E731
    first = Stream()
    pp.publish_pin(tmp_path, "nikke-installer", resolve=resolve, open_stream=first, block_size=BS)
    replaced = DATA[:5000] + b"NEW!" + DATA[5004:]
    second = Stream(replaced)
    assert (
        pp.publish_pin(
            tmp_path, "nikke-installer", resolve=resolve, open_stream=second, block_size=BS
        )
        == "published"
    )
    doc = json.loads((tmp_path / "catalog" / "archive-pins" / "nikke-installer.json").read_text())
    pins = archivepins.parse_document(doc)
    assert pins[installerpins.ARCHIVE] == pin(NIKKE_URL, replaced)
