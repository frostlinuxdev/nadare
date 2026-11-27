# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline coverage for Heaven Burns Red's shared Yostar route."""

import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from _backend_helpers import make_context, write_exe

from nadare.core import backends, directdl, storecatalog, yostar
from nadare.core import download as dl
from nadare.core.backends import BackendError

ROOT = Path(__file__).resolve().parents[1]
HOST = "https://launcher-pkg-hbr-en.yo-star.com"
SALT = "test-salt"
TAGS = ("StellaSora_EN", "Arknights_EN", "HBR_EN")


def generator():
    spec = importlib.util.spec_from_file_location(
        "hbr_generator", ROOT / "ci/yostar-manifests/generate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def backend(tmp_path, profile=None):
    return backends.get("hbr-yostar")(make_context(tmp_path, "hbr-yostar", profile or {}))


def manifest():
    return json.loads((ROOT / "tests/fixtures/catalog/hbr_manifest.json").read_text())


def clear_yostar_env(monkeypatch, gen):
    for tag in gen.GAME_TAGS:
        monkeypatch.delenv(gen._env_key("SALT_", tag), raising=False)
        monkeypatch.delenv(gen._env_key("VERSION_", tag), raising=False)


class FakeYostar:
    """The launcher API, keyed by the signed game tag; records every request."""

    def __init__(self, *, fail=()):
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.fail = set(fail)

    @staticmethod
    def tag(headers):
        return json.loads(headers["Authorization"])["head"]["game_tag"]

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url.startswith("https://cdn.yo-star.com/"):
            # Step 3 is unsigned and public, so it carries no game tag.
            return json.dumps(
                {
                    "source": "prod/HBR_EN",
                    "file": [
                        {"path": "Game.exe", "size": 2, "hash": str(yostar.crc64(b"MZ"))}
                    ],
                }
            ).encode()
        tag = self.tag(headers)
        if tag in self.fail:
            raise OSError(f"upstream {tag} is down")
        if url.endswith("/api/launcher/game/config"):
            payload = {
                "data": {
                    "game_latest_version": "4.0.3",
                    "game_latest_file_path": f"prod/{tag}/m.zip",
                }
            }
        elif "/api/launcher/game/config/json?" in url:
            payload = {"data": {"url": f"https://cdn.yo-star.com/{tag}/manifest.json"}}
        elif url.endswith("/api/launcher/base/config"):
            payload = {
                "data": {
                    "launcher_background_img": f"https://launcher-pkg-{tag}.yo-star.com/bg.jpg"
                }
            }
        elif url.endswith("/api/launcher/operations/resource"):
            payload = {"data": {"operations_banner_list": []}}
        else:  # pragma: no cover - a new request means the test must grow
            raise AssertionError(f"unexpected URL: {url}")
        return json.dumps(payload).encode()

    def for_tag(self, tag):
        return [
            call
            for call in self.calls
            if "Authorization" in call[1] and self.tag(call[1]) == tag
        ]

    def auth_headers(self):
        return [headers for _url, headers in self.calls if "Authorization" in headers]


def expected_sign(head, salt=SALT):
    """The launcher's signing rule, written out here: md5(head + body + salt)."""
    return hashlib.md5(
        (json.dumps(head, separators=(",", ":")) + "" + salt).encode("utf-8")
    ).hexdigest()


def test_hbr_identity_and_catalog(tmp_path):
    assert yostar.GAME_TAGS["HBR_EN"] == "hbr-yostar"
    game = backend(tmp_path)
    assert game.kind == "hbr-yostar"
    assert game.game_tag == "HBR_EN"
    assert game.display_name == "Heaven Burns Red"
    assert game.install_dir() == game.prefix() / "drive_c/YostarGames/HeavenBurnsRed_EN"
    assert game.direct_exe == "HeavenBurnsRed.exe"
    assert game.launcher_names == ("HeavenBurnsRed.exe",)
    assert game.install_size_hint == 38_706_311_237
    assert game.direct_mode() is True
    assert game.news_source is None  # no unrelated Arknights feed
    assert game.official_media is True
    row = next(row for row in storecatalog.entries() if row.kind == "hbr-yostar")
    assert row.tier == "Experimental"
    assert row.title == "Heaven Burns Red"
    assert row.has_art is True  # the Store asks the index for its cover
    from nadare.gui.add_game import PREFIX_KINDS, _catalog

    wizard = next(row for row in _catalog() if row.kind == "hbr-yostar")
    assert "hbr-yostar" in PREFIX_KINDS
    assert wizard.install_subdir == "HeavenBurnsRed_EN"
    assert wizard.exes == ("HeavenBurnsRed.exe",)
    assert wizard.badge == "Experimental"


def test_the_update_check_needs_a_configured_hbr_manifest(tmp_path, monkeypatch):
    """HBR keeps its background update check only with a published manifest (#1786 review)."""
    from nadare.gui.main_window import MainWindow

    monkeypatch.delenv("YOSTAR_MANIFEST_INDEX", raising=False)
    monkeypatch.setattr(yostar.hosting, "yostar_index_url", lambda: "")
    file_manifest = tmp_path / "manifest.json"
    window = SimpleNamespace(
        config=SimpleNamespace(
            profiles={
                "hbr-off": {"backend": "hbr-yostar"},
                "hbr-on": {"backend": "hbr-yostar", "manifest_file": str(file_manifest)},
                "ak-off": {"backend": "arknights-yostar"},
            }
        )
    )
    assert MainWindow._has_direct_manifest(window, "hbr-off") is False
    assert MainWindow._has_direct_manifest(window, "hbr-on") is True
    assert MainWindow._has_direct_manifest(window, "ak-off") is False


def test_published_keys_roundtrip_and_legacy_names(tmp_path):
    gen = generator()
    manifests = [
        dict(manifest(), game_tag=tag) for tag in ("Arknights_EN", "StellaSora_EN", "HBR_EN")
    ]
    media = {
        m["game_tag"]: {"game_tag": m["game_tag"], "background": None, "banners": []}
        for m in manifests
    }
    index = gen.write_outputs(manifests, tmp_path, media=media)
    assert set(index) == {"Arknights_EN", "StellaSora_EN", "hbr-yostar"}
    for tag, key in (
        ("Arknights_EN", "Arknights_EN"),
        ("StellaSora_EN", "StellaSora_EN"),
        ("HBR_EN", "hbr-yostar"),
    ):
        assert index[key]["url"] == key + ".json"
        assert index[key]["media"] == key + ".media.json"
        assert yostar.load_manifest(str(tmp_path / "index.json"), game_tag=tag).game_tag == tag
        assert yostar.load_media(str(tmp_path / "index.json"), game_tag=tag).game_tag == tag
        assert (
            index[key]["sha256"]
            == hashlib.sha256((tmp_path / (key + ".json")).read_bytes()).hexdigest()
        )


def test_generator_skips_only_hbr_without_its_salt(tmp_path, monkeypatch, capsys):
    """Missing SALT_HBR_EN must not wedge Arknights and Stella Sora (exit 0)."""
    gen = generator()
    clear_yostar_env(monkeypatch, gen)
    fake = FakeYostar()
    monkeypatch.setattr(gen, "_http_get", fake)
    for tag in ("StellaSora_EN", "Arknights_EN"):
        monkeypatch.setenv(gen._env_key("SALT_", tag), SALT)
        monkeypatch.setenv(gen._env_key("VERSION_", tag), "1.4.1")
    monkeypatch.setenv("VERSION_HBR_EN", "1.4.1")  # version present, salt missing

    out = tmp_path / "fresh"
    published = tmp_path / "published"
    assert gen.main(["--out", str(out), "--merge-into", str(published)]) == 0
    captured = capsys.readouterr()
    assert "skip HBR_EN: no SALT_HBR_EN in the environment" in captured.err
    assert "not yet published" not in captured.err
    assert not (out / "hbr-yostar.json").exists()
    assert (out / "StellaSora_EN.json").is_file()
    assert (out / "Arknights_EN.json").is_file()
    index = json.loads((published / "index.json").read_text())
    assert set(index) == {"StellaSora_EN", "Arknights_EN"}

    # Every signed call (HBR never made one) carries a correct md5 signature, and
    # Arknights/Stella Sora signatures are exactly what their salt produces.
    assert {FakeYostar.tag(h) for h in fake.auth_headers()} == {
        "StellaSora_EN",
        "Arknights_EN",
    }
    for headers in fake.auth_headers():
        auth = json.loads(headers["Authorization"])
        assert auth["sign"] == expected_sign(auth["head"])

    # The salt never reaches the header, the console or any written file.
    for headers in fake.auth_headers():
        assert SALT not in json.dumps(headers)
    for text in (captured.out, captured.err):
        assert SALT not in text
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SALT not in path.read_text(encoding="utf-8", errors="replace"), path


def test_generator_publishes_hbr_manifest_and_media(tmp_path, monkeypatch, capsys):
    gen = generator()
    clear_yostar_env(monkeypatch, gen)
    fake = FakeYostar()
    monkeypatch.setattr(gen, "_http_get", fake)
    for tag in TAGS:
        monkeypatch.setenv(gen._env_key("SALT_", tag), SALT)
        monkeypatch.setenv(gen._env_key("VERSION_", tag), "1.4.1")

    out = tmp_path / "out"
    assert gen.main(["--out", str(out), "--now", "2026-11-23T00:00:00+00:00"]) == 0
    index = json.loads((out / "index.json").read_text())
    assert set(index) == {"StellaSora_EN", "Arknights_EN", "hbr-yostar"}
    assert index["hbr-yostar"]["url"] == "hbr-yostar.json"
    assert index["hbr-yostar"]["media"] == "hbr-yostar.media.json"
    assert json.loads((out / "hbr-yostar.json").read_text())["game_tag"] == "HBR_EN"
    media = json.loads((out / "hbr-yostar.media.json").read_text())
    assert media["game_tag"] == "HBR_EN"

    hbr = fake.for_tag("HBR_EN")
    queries = [url for url, _h in hbr if "/api/launcher/game/config/json?" in url]
    assert queries == [
        gen.API_BASE
        + "/api/launcher/game/config/json?version=4.0.3&file_path=prod/HBR_EN/m.zip"
    ]
    for _url, headers in hbr:
        auth = json.loads(headers["Authorization"])
        assert auth["head"]["game_tag"] == "HBR_EN"
        assert auth["head"]["version"] == "1.4.1"
        assert auth["sign"] == expected_sign(auth["head"])
        assert SALT not in json.dumps(headers)
    captured = capsys.readouterr()
    for text in (captured.out, captured.err):
        assert SALT not in text
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SALT not in path.read_text(encoding="utf-8", errors="replace"), path


def test_merge_keeps_last_good_hbr_when_hbr_fails(tmp_path, monkeypatch, capsys):
    gen = generator()
    clear_yostar_env(monkeypatch, gen)
    for tag in TAGS:
        monkeypatch.setenv(gen._env_key("SALT_", tag), SALT)
        monkeypatch.setenv(gen._env_key("VERSION_", tag), "1.4.1")
    published = tmp_path / "published"
    monkeypatch.setattr(gen, "_http_get", FakeYostar())
    assert (
        gen.main(["--out", str(tmp_path / "first"), "--merge-into", str(published)]) == 0
    )
    before_index = json.loads((published / "index.json").read_text())
    before_hbr = (published / "hbr-yostar.json").read_bytes()
    before_media = (published / "hbr-yostar.media.json").read_bytes()

    # A later run: HBR's upstream fails, the others still generate.
    monkeypatch.setattr(gen, "_http_get", FakeYostar(fail={"HBR_EN"}))
    assert (
        gen.main(["--out", str(tmp_path / "second"), "--merge-into", str(published)]) == 0
    )
    after_index = json.loads((published / "index.json").read_text())
    assert after_index["hbr-yostar"] == before_index["hbr-yostar"]
    assert after_index["hbr-yostar"]["media"] == "hbr-yostar.media.json"
    assert (published / "hbr-yostar.json").read_bytes() == before_hbr
    assert (published / "hbr-yostar.media.json").read_bytes() == before_media
    assert "hbr-yostar: not refreshed this run, keeping the published entry" in (
        capsys.readouterr().err
    )


def test_merge_fails_closed_if_hbr_would_be_dropped(tmp_path, monkeypatch, capsys):
    gen = generator()
    clear_yostar_env(monkeypatch, gen)
    for tag in ("Arknights_EN", "HBR_EN"):
        monkeypatch.setenv(gen._env_key("SALT_", tag), SALT)
        monkeypatch.setenv(gen._env_key("VERSION_", tag), "1.4.1")
    monkeypatch.setattr(gen, "_http_get", FakeYostar(fail={"HBR_EN"}))
    published = tmp_path / "published"
    published.mkdir()
    (published / "index.json").write_text(
        json.dumps(
            {
                "Arknights_EN": {"version": "1", "url": "Arknights_EN.json", "sha256": "x"},
                "hbr-yostar": {"version": "1", "url": "hbr-yostar.json", "sha256": "x"},
            }
        )
    )
    monkeypatch.setattr(
        gen,
        "merge_outputs",
        lambda _fresh, _published: (
            {"Arknights_EN": {"version": "1", "url": "Arknights_EN.json", "sha256": "x"}},
            ["Arknights_EN"],
            [],
        ),
    )
    assert (
        gen.main(["--out", str(tmp_path / "fresh"), "--merge-into", str(published)]) == 1
    )
    assert "merge dropped previously published tag(s): hbr-yostar" in capsys.readouterr().err


def test_hbr_plan_host_size_and_update(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()))
    game = backend(tmp_path, {"manifest_file": str(path)})
    plan = game.plan()
    assert plan.game_tag == "HBR_EN"
    assert plan.exe == "HeavenBurnsRed.exe"
    assert len(plan.files) == 2
    assert sum(f.size for f in plan.files) == 5
    assert plan.files[0].url == HOST + "/prod/HBR_EN/HeavenBurnsRed.exe"
    assert plan.files[0].crc64 == yostar.crc64(b"MZ")
    target = tmp_path / "installed"
    target.mkdir()
    (target / "HeavenBurnsRed.exe").write_bytes(b"MZ")
    missing, archives = directdl.plan_missing(plan, target, verify=True)
    assert [f.path for f in missing] == ["HeavenBurnsRed_Data/app.info"]
    assert archives == []
    newer = manifest()
    newer["version"] = "4.0.4"
    path.write_text(json.dumps(newer))
    assert game.plan().version == "4.0.4"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://evil.example/prod/HBR_EN",
        "https://evil-yo-star.com/prod/HBR_EN",
        "http://launcher-pkg-hbr-en.yo-star.com/prod/HBR_EN",
    ],
)
def test_hbr_plan_refuses_untrusted_download_hosts(tmp_path, base_url):
    payload = manifest()
    payload["base_url"] = base_url
    path = tmp_path / "bad-host.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(BackendError, match="invalid or incomplete"):
        backend(tmp_path, {"manifest_file": str(path)}).plan()


def test_hbr_redirects_stay_on_the_yostar_suffix(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()))
    guard = backend(tmp_path, {"manifest_file": str(path)}).plan().redirect_allowed
    assert guard is not None
    assert guard(HOST + "/prod/HBR_EN/HeavenBurnsRed.exe")
    assert guard("https://api-launcher-en.yo-star.com/api/launcher/game/config")
    assert not guard("https://evil.example/HeavenBurnsRed.exe")
    assert not guard("https://evil-yo-star.com/HeavenBurnsRed.exe")


@pytest.mark.parametrize(
    "change",
    [
        {"files": "bad"},
        {"version": None},
        {"files": [{"path": "../escape", "size": 2, "crc64": 0}]},
        {"files": [{"path": "/escape", "size": 2, "crc64": 0}]},
        {"files": [{"path": "a", "size": -1, "crc64": 0}]},
        {"files": [{"path": "a", "size": 2, "crc64": "bad-crc"}]},
        {"files": [{"size": 2, "crc64": 0}]},
    ],
)
def test_malformed_hbr_refused(tmp_path, change):
    payload = copy.deepcopy(manifest())
    payload.update(change)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(BackendError, match="invalid or incomplete"):
        backend(tmp_path, {"manifest_file": str(path)}).plan()


def test_no_manifest_refuses_before_setup_but_still_plays(tmp_path, monkeypatch):
    from nadare.backends.arknights import INSTALLED_PREFIX_KEY, ArknightsBackend

    monkeypatch.delenv("YOSTAR_MANIFEST_INDEX", raising=False)
    monkeypatch.setattr(yostar.hosting, "yostar_index_url", lambda: "")
    game = backend(tmp_path)
    monkeypatch.setattr(
        type(game), "_ensure_default_runner", lambda *a, **kw: pytest.fail("runner setup")
    )
    for action in (game.install, game.update):
        with pytest.raises(
            BackendError, match="Heaven Burns Red.*published manifest index.*turned off"
        ):
            action()
    with pytest.raises(
        BackendError, match="Heaven Burns Red.*published manifest index.*turned off"
    ):
        game.plan()
    assert INSTALLED_PREFIX_KEY not in game.ctx.profile
    assert not game.install_dir().exists()
    # A background update check without a manifest stays quiet, with no download attempt.
    with monkeypatch.context() as quiet:
        quiet.setattr(yostar, "load_manifest", lambda *a, **kw: pytest.fail("update fetch"))
        quiet.setattr(directdl, "sync", lambda *a, **kw: pytest.fail("update sync"))
        assert game.check_update() is None
    # An installed game still launches: launching needs no manifest.
    write_exe(game.install_dir() / game.direct_exe, "MZ")
    monkeypatch.setattr(type(game), "_ensure_default_runner", lambda *a, **kw: None)
    launched: list[str] = []
    monkeypatch.setattr(
        type(game), "_launch_direct", lambda self: launched.append("direct") or 0
    )
    assert game.launch() == 0
    assert launched == ["direct"]
    assert isinstance(game, ArknightsBackend)
    # An index with no HBR entry follows the same invalid-manifest refusal as Arknights.
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"Arknights_EN": {"url": "Arknights_EN.json"}}))
    game.ctx.profile["manifest_file"] = str(path)
    with pytest.raises(BackendError, match="invalid or incomplete"):
        game.plan()


def test_publisher_keeps_hbr_and_existing_filenames(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "hbr_publisher", ROOT / "scripts/publish_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        gen = generator()
        out = tmp_path / "published"
        media_payload = lambda tag: {"game_tag": tag, "background": None, "banners": []}  # noqa: E731
        existing = [dict(manifest(), game_tag=tag) for tag in ("StellaSora_EN", "Arknights_EN")]
        gen.write_outputs(
            existing, out, media={m["game_tag"]: media_payload(m["game_tag"]) for m in existing}
        )
        state = {
            "env": {
                "SALT_STELLASORA_EN": SALT,
                "SALT_ARKNIGHTS_EN": SALT,
                "SALT_HBR_EN": SALT,
                "VERSION_HBR_EN": "1.4.1",
            },
            "tags": ("StellaSora_EN", "Arknights_EN", "HBR_EN"),
        }

        def run(argv, *, check, env):
            assert check is True
            assert env == state["env"]
            assert argv[-2] == "--out"
            manifests = [dict(manifest(), game_tag=tag) for tag in state["tags"]]
            gen.write_outputs(
                manifests,
                argv[-1],
                media={m["game_tag"]: media_payload(m["game_tag"]) for m in manifests},
            )

        monkeypatch.setattr(
            module,
            "subprocess",
            SimpleNamespace(
                run=run, CalledProcessError=subprocess.CalledProcessError
            ),
        )
        assert module.publish_yostar(out, state["env"]) is True
        index = json.loads((out / "index.json").read_text())
        assert set(index) == {"StellaSora_EN", "Arknights_EN", "hbr-yostar"}
        assert index["Arknights_EN"]["url"] == "Arknights_EN.json"
        assert index["StellaSora_EN"]["url"] == "StellaSora_EN.json"
        assert index["hbr-yostar"]["url"] == "hbr-yostar.json"
        assert index["hbr-yostar"]["media"] == "hbr-yostar.media.json"
        for tag in ("Arknights_EN", "StellaSora_EN", "hbr-yostar"):
            assert (out / f"{tag}.json").is_file()
            assert (out / f"{tag}.media.json").is_file()
        before = (out / "hbr-yostar.json").read_bytes()

        # A run without SALT_HBR_EN publishes no HBR and keeps the existing entry and files.
        state["env"] = {"SALT_STELLASORA_EN": SALT, "SALT_ARKNIGHTS_EN": SALT}
        state["tags"] = ("StellaSora_EN", "Arknights_EN")
        assert module.publish_yostar(out, state["env"]) is False
        kept = json.loads((out / "index.json").read_text())
        assert kept["hbr-yostar"] == index["hbr-yostar"]
        assert (out / "hbr-yostar.json").read_bytes() == before
        assert (out / "hbr-yostar.media.json").is_file()
    finally:
        sys.modules.pop(spec.name, None)


def test_hbr_says_its_version_is_the_download_package(tmp_path):
    """The feed's ``game_latest_version`` is the package Nadare downloads, not the game's build.

    HBR's own title screen said 4.0.2(72) while the Yostar feed said 4.0.3 (real run, 11-24),
    so the page must not present the download number as the build the game runs.
    """
    game = backend(tmp_path)
    target = game.install_dir() / "HeavenBurnsRed.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"MZ")
    game._write_version_file("4.0.3", "HBR_EN")

    additions, version, early = game._direct_status_parts(target, False)

    assert early is None
    assert version == "4.0.3"
    assert additions["version"] == "4.0.3"
    assert additions["version_kind"] == "download"


def test_the_public_hbr_status_carries_the_download_kind(tmp_path):
    """Through ``status()``, as the page and the CLI read it, not a private helper."""
    game = backend(tmp_path)
    target = game.install_dir() / "HeavenBurnsRed.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"MZ")
    game._write_version_file("4.0.3", "HBR_EN")

    status = game.status()

    assert status.state == "installed"
    assert status.version == "4.0.3"
    assert status.data["version_kind"] == "download"


def test_a_backend_without_the_flag_omits_the_download_kind(tmp_path):
    """HBR's sibling shares the code; only HBR's number is a download package's."""
    game = backends.get("arknights-yostar")(
        make_context(tmp_path, "arknights-yostar", {})
    )
    target = game.install_dir() / game.direct_exe
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"MZ")
    game._write_version_file("4.0.3", "Arknights_EN")

    status = game.status()

    assert status.version == "4.0.3"
    assert "version_kind" not in status.data


def test_hbr_install_and_update_through_the_real_sync(tmp_path, monkeypatch):
    from nadare.backends.winegame import WineGameBackend

    monkeypatch.setattr(WineGameBackend, "_ensure_default_runner", lambda *a, **kw: None)
    monkeypatch.setattr(WineGameBackend, "_ensure_direct_prefix", lambda *a: None)
    contents = {"HeavenBurnsRed.exe": b"MZ", "HeavenBurnsRed_Data/app.info": b"hbr"}
    requests: list[str] = []

    class Body(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.status = 200
            self.headers = {"Content-Length": str(len(data))}

        def getheader(self, name, default=None):
            return self.headers.get(name, default)

        def isclosed(self):
            return self.closed

    def open_(self, url, **kwargs):
        requests.append(url)
        relative = url.removeprefix(HOST + "/prod/HBR_EN/")
        return Body(contents[relative])

    # Only the transport is fake: directdl.sync and its CRC-64 check are the real ones.
    monkeypatch.setattr(dl.HttpClientPool, "open", open_)
    payload = manifest()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload))
    game = backend(tmp_path, {"manifest_file": str(path)})
    game.install()
    assert game.status().state == "installed"
    assert sorted(requests) == [
        HOST + "/prod/HBR_EN/HeavenBurnsRed.exe",
        HOST + "/prod/HBR_EN/HeavenBurnsRed_Data/app.info",
    ]
    info = game.install_dir() / "HeavenBurnsRed_Data/app.info"
    assert info.read_bytes() == b"hbr"

    requests.clear()
    info.write_bytes(b"old")  # same size, wrong CRC-64: only this file is re-fetched
    payload["version"] = "4.0.4"
    path.write_text(json.dumps(payload))
    game.update()
    assert requests == [HOST + "/prod/HBR_EN/HeavenBurnsRed_Data/app.info"]
    assert info.read_bytes() == b"hbr"
    assert game._read_version_file()["version"] == "4.0.4"
