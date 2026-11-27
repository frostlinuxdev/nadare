# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline coverage for Azur Promilia (JP)'s shared Yostar route."""

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
HOST = "https://launcher-pkg-azupro-jp.yo-star.com"
BACKUP_HOST = "https://launcher-pkg-azupro-jp-bk.yo-star.com"
API_HOST = "https://api-launcher-jp.yo-star.com"
EN_HOST = "https://api-launcher-en.yo-star.com"
API_HOSTS = {
    "StellaSora_EN": EN_HOST,
    "Arknights_EN": EN_HOST,
    "HBR_EN": EN_HOST,
    "AZUPRO_JP": API_HOST,
}
SALT = "test-salt"
TAGS = ("StellaSora_EN", "Arknights_EN", "HBR_EN", "AZUPRO_JP")


def generator():
    spec = importlib.util.spec_from_file_location(
        "azupro_generator", ROOT / "ci/yostar-manifests/generate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def backend(tmp_path, profile=None):
    return backends.get("azurpromilia-jp-yostar")(
        make_context(tmp_path, "azurpromilia-jp-yostar", profile or {})
    )


def manifest():
    return json.loads((ROOT / "tests/fixtures/catalog/azupro_manifest.json").read_text())


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
                    "source": "prod/AZUPRO_JP",
                    "file": [
                        {"path": "AzurPromiliaJP.exe", "size": 2, "hash": str(yostar.crc64(b"MZ"))}
                    ],
                }
            ).encode()
        tag = self.tag(headers)
        expected = API_HOSTS[tag]
        assert url.startswith(expected + "/api/launcher/"), (tag, url)
        if tag in self.fail:
            raise OSError(f"upstream {tag} is down")
        if url.endswith("/api/launcher/game/config"):
            payload = {
                "data": {
                    "game_latest_version": "1.0.1",
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


def test_azupro_identity_and_catalog(tmp_path):
    assert yostar.GAME_TAGS["AZUPRO_JP"] == "azurpromilia-jp-yostar"
    assert yostar.index_key("AZUPRO_JP") == "azurpromilia-jp-yostar"
    game = backend(tmp_path)
    assert game.kind == "azurpromilia-jp-yostar"
    assert game.game_tag == "AZUPRO_JP"
    assert game.display_name == "Azur Promilia (JP)"
    assert game.install_dir() == game.prefix() / "drive_c/YostarGames/AZUPRO_JP"
    assert game.direct_exe == "AzurPromiliaJP.exe"
    assert game.launcher_names == ("AzurPromiliaJP.exe",)
    assert game.install_size_hint == 84_158_595_039
    assert game.direct_mode() is True
    assert game.news_source is None  # no unrelated feed
    assert game.official_media is True
    assert game.default_display == "x11"
    assert game.dxvk_config == ""
    display = next(option for option in game.options if option.key == "display")
    assert display.choices[0] == ("Auto (X11)", "auto")
    assert game.official_image_hosts == (
        "webusstatic.yo-star.com",
        "launcher-pkg-azupro-jp.yo-star.com",
    )
    row = next(row for row in storecatalog.entries() if row.kind == "azurpromilia-jp-yostar")
    assert row.tier == "Experimental"
    assert row.title == "Azur Promilia (JP)"
    assert row.has_art is True  # the Store asks the index for its cover
    from nadare.gui.add_game import PREFIX_KINDS, _catalog

    wizard = next(row for row in _catalog() if row.kind == "azurpromilia-jp-yostar")
    assert "azurpromilia-jp-yostar" in PREFIX_KINDS
    assert wizard.install_subdir == "AZUPRO_JP"
    assert wizard.exes == ("AzurPromiliaJP.exe",)
    assert wizard.badge == "Experimental"

    # Registration points a new Yostar game needs (HBR's list, #1786).
    from nadare.core import artindex, installercache, profilenames, runners

    assert artindex.ALLOWED_HOSTS["azurpromilia-jp-yostar"] == (
        "webusstatic.yo-star.com",
        "launcher-pkg-azupro-jp.yo-star.com",
    )
    assert profilenames.short_name("azurpromilia-jp-yostar") == "azur-promilia"
    assert "azurpromilia-jp-yostar" in runners.PROTON_DEFAULT_BACKENDS
    assert installercache.needed_folders(
        {"p": {"backend": "azurpromilia-jp-yostar"}}
    ) == {"arknights-yostar"}


def test_the_page_notice_warns_before_the_big_download(tmp_path):
    game = backend(tmp_path)
    notice = game.page_notice
    assert notice is not None
    text, url, label = notice
    assert "Japanese client" in text
    # The same formatter and unit as its Store card (84,158,595,039 B).
    from nadare.core.diskspace import human_size

    assert f"about {human_size(game.install_size_hint)}" in text
    assert "GB" not in text
    assert "Japanese Yostar account" in text
    assert "anti-cheat" in text
    assert "untested on Linux" in text
    assert "may not start" in text
    assert url == "https://azurpromilia.jp/"
    assert label == "Official site"
    row = next(row for row in storecatalog.entries() if row.kind == "azurpromilia-jp-yostar")
    assert row.notice == notice


def test_azupro_default_launch_args_and_user_override(tmp_path):
    target = Path("/games/AzurPromiliaJP.exe")
    game = backend(tmp_path)
    assert game.launch_extra_args() == ["-start=azurpromilia_launcher"]
    assert game._direct_argv(target) == [str(target), "-start=azurpromilia_launcher"]

    # A profile that sets its own launch_args keeps control: no default is added.
    own = backend(tmp_path, {"launch_args": "-windowed -nosplash"})
    assert own.launch_extra_args() == []
    assert own._direct_argv(target) == [str(target), "-windowed", "-nosplash"]


@pytest.mark.parametrize(
    "profile, user_args",
    [
        ({"launch_args": ["-windowed", "-nosplash"]}, ["-windowed", "-nosplash"]),
        ({"extra": {"launch_args": "-windowed -nosplash"}}, ["-windowed", "-nosplash"]),
        ({"launch_args": "   "}, ["-start=azurpromilia_launcher"]),
        ({"extra": {"launch_args": []}}, ["-start=azurpromilia_launcher"]),
    ],
)
def test_user_launch_args_forms_replace_or_keep_the_default(tmp_path, profile, user_args):
    target = Path("/games/AzurPromiliaJP.exe")
    game = backend(tmp_path, profile)
    assert game._direct_argv(target) == [str(target), *user_args]


def test_launch_reaches_the_executed_argv_with_the_start_arg(tmp_path, monkeypatch):
    """The default start param survives prefixing and reaches the process that is spawned."""
    import contextlib
    import threading

    from nadare.backends.winegame_parts import launching

    game = backend(tmp_path)
    target = game.install_dir() / game.direct_exe
    write_exe(target, "MZ")
    monkeypatch.setattr(type(game), "_ensure_default_runner", lambda *a, **kw: None)
    monkeypatch.setattr(type(game), "_run_vcredist", lambda *a, **kw: None)
    monkeypatch.setattr(
        type(game),
        "_prefix_command",
        lambda self, args, env=None, **kw: (["umu-run", *args], {}),
    )
    monkeypatch.setattr(type(game), "_secure_wrap", lambda self, argv, env: (argv, env))
    monkeypatch.setattr(type(game), "_record_polite_stop", lambda *a, **kw: None)
    monkeypatch.setattr(type(game), "_run_lock", lambda self: contextlib.nullcontext())
    monkeypatch.setattr(type(game), "_log_launch_details", lambda *a, **kw: None)
    monkeypatch.setattr(type(game), "_runtime_note", lambda self: "fake runner")
    monkeypatch.setattr(type(game), "_runner_facts", lambda self: {})
    monkeypatch.setattr(launching.graphics, "effective_renderer", lambda profile: (None, ""))
    probe = SimpleNamespace(result="", stop=lambda: None, join=lambda **kw: None)
    probe.start = lambda: probe
    monkeypatch.setattr(type(game), "_renderer_probe", lambda self: probe)
    closed = threading.Event()
    closed.set()

    @contextlib.contextmanager
    def watch(_self):
        yield closed

    monkeypatch.setattr(type(game), "_game_process_watch", watch)
    monkeypatch.setattr(type(game), "_begin_game_output", lambda self: None)
    monkeypatch.setattr(type(game), "_end_game_output", lambda self: None)
    monkeypatch.setattr(type(game), "_stop_link_relay", lambda self: None)
    executed: list[list[str]] = []

    def spawn(argv, env, **kwargs):
        executed.append(list(argv))
        return 0

    monkeypatch.setattr(launching, "stream", spawn)
    assert game.launch() == 0
    assert executed == [["umu-run", str(target), "-start=azurpromilia_launcher"]]


def test_the_update_check_needs_a_configured_azupro_manifest(tmp_path, monkeypatch):
    from nadare.gui.main_window import MainWindow

    monkeypatch.delenv("YOSTAR_MANIFEST_INDEX", raising=False)
    monkeypatch.setattr(yostar.hosting, "yostar_index_url", lambda: "")
    file_manifest = tmp_path / "manifest.json"
    window = SimpleNamespace(
        config=SimpleNamespace(
            profiles={
                "azupro-off": {"backend": "azurpromilia-jp-yostar"},
                "azupro-on": {
                    "backend": "azurpromilia-jp-yostar",
                    "manifest_file": str(file_manifest),
                },
                "ak-off": {"backend": "arknights-yostar"},
            }
        )
    )
    assert MainWindow._has_direct_manifest(window, "azupro-off") is False
    assert MainWindow._has_direct_manifest(window, "azupro-on") is True
    assert MainWindow._has_direct_manifest(window, "ak-off") is False


def test_published_keys_roundtrip_and_legacy_names(tmp_path):
    gen = generator()
    manifests = [dict(manifest(), game_tag=tag) for tag in TAGS]
    media = {
        m["game_tag"]: {"game_tag": m["game_tag"], "background": None, "banners": []}
        for m in manifests
    }
    index = gen.write_outputs(manifests, tmp_path, media=media)
    assert set(index) == {
        "Arknights_EN",
        "StellaSora_EN",
        "hbr-yostar",
        "azurpromilia-jp-yostar",
    }
    for tag, key in (
        ("Arknights_EN", "Arknights_EN"),
        ("StellaSora_EN", "StellaSora_EN"),
        ("HBR_EN", "hbr-yostar"),
        ("AZUPRO_JP", "azurpromilia-jp-yostar"),
    ):
        assert index[key]["url"] == key + ".json"
        assert index[key]["media"] == key + ".media.json"
        assert yostar.load_manifest(str(tmp_path / "index.json"), game_tag=tag).game_tag == tag
        assert yostar.load_media(str(tmp_path / "index.json"), game_tag=tag).game_tag == tag
        assert (
            index[key]["sha256"]
            == hashlib.sha256((tmp_path / (key + ".json")).read_bytes()).hexdigest()
        )


def test_generator_skips_only_azupro_without_its_salt(tmp_path, monkeypatch, capsys):
    """Missing SALT_AZUPRO_JP must not wedge the other games (exit 0)."""
    gen = generator()
    clear_yostar_env(monkeypatch, gen)
    fake = FakeYostar()
    monkeypatch.setattr(gen, "_http_get", fake)
    for tag in ("StellaSora_EN", "Arknights_EN"):
        monkeypatch.setenv(gen._env_key("SALT_", tag), SALT)
        monkeypatch.setenv(gen._env_key("VERSION_", tag), "1.8.1")
    monkeypatch.setenv("VERSION_AZUPRO_JP", "1.8.1")  # version present, salt missing

    out = tmp_path / "fresh"
    published = tmp_path / "published"
    assert gen.main(["--out", str(out), "--merge-into", str(published)]) == 0
    captured = capsys.readouterr()
    assert "skip AZUPRO_JP: no SALT_AZUPRO_JP in the environment" in captured.err
    assert "not yet published" not in captured.err
    assert not (out / "azurpromilia-jp-yostar.json").exists()
    assert (out / "StellaSora_EN.json").is_file()
    assert (out / "Arknights_EN.json").is_file()
    index = json.loads((published / "index.json").read_text())
    assert set(index) == {"StellaSora_EN", "Arknights_EN"}

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


def test_generator_publishes_azupro_manifest_and_media(tmp_path, monkeypatch, capsys):
    gen = generator()
    clear_yostar_env(monkeypatch, gen)
    fake = FakeYostar()
    monkeypatch.setattr(gen, "_http_get", fake)
    for tag in TAGS:
        monkeypatch.setenv(gen._env_key("SALT_", tag), SALT)
        monkeypatch.setenv(gen._env_key("VERSION_", tag), "1.8.1")

    out = tmp_path / "out"
    assert gen.main(["--out", str(out), "--now", "2026-11-24T00:00:00+00:00"]) == 0
    index = json.loads((out / "index.json").read_text())
    assert set(index) == {
        "StellaSora_EN",
        "Arknights_EN",
        "hbr-yostar",
        "azurpromilia-jp-yostar",
    }
    assert index["azurpromilia-jp-yostar"]["url"] == "azurpromilia-jp-yostar.json"
    assert index["azurpromilia-jp-yostar"]["media"] == "azurpromilia-jp-yostar.media.json"
    assert (
        json.loads((out / "azurpromilia-jp-yostar.json").read_text())["game_tag"] == "AZUPRO_JP"
    )
    media = json.loads((out / "azurpromilia-jp-yostar.media.json").read_text())
    assert media["game_tag"] == "AZUPRO_JP"

    # Existing games keep their published file names.
    for key in ("Arknights_EN", "StellaSora_EN", "hbr-yostar"):
        assert index[key]["url"] == key + ".json"

    # Every signed call hits the API host of its own tag, and the endpoints are exact.
    for tag, host in API_HOSTS.items():
        signed = [url for url, _h in fake.for_tag(tag)]
        assert signed, tag
        assert all(url.startswith(host + "/api/launcher/") for url in signed), (tag, signed)
        queries = [url for url in signed if "/api/launcher/game/config/json?" in url]
        assert queries == [
            host + f"/api/launcher/game/config/json?version=1.0.1&file_path=prod/{tag}/m.zip"
        ]
        assert {url for url in signed} >= {
            host + "/api/launcher/game/config",
            host + "/api/launcher/base/config",
            host + "/api/launcher/operations/resource",
        }
    azupro = fake.for_tag("AZUPRO_JP")
    assert not any("api-launcher-en" in url for url, _h in azupro)
    for _url, headers in azupro:
        auth = json.loads(headers["Authorization"])
        assert auth["head"]["game_tag"] == "AZUPRO_JP"
        assert auth["head"]["version"] == "1.8.1"
        assert auth["sign"] == expected_sign(auth["head"])
        assert SALT not in json.dumps(headers)
    captured = capsys.readouterr()
    for text in (captured.out, captured.err):
        assert SALT not in text
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SALT not in path.read_text(encoding="utf-8", errors="replace"), path


def test_merge_keeps_last_good_azupro_when_azupro_fails(tmp_path, monkeypatch, capsys):
    gen = generator()
    clear_yostar_env(monkeypatch, gen)
    for tag in TAGS:
        monkeypatch.setenv(gen._env_key("SALT_", tag), SALT)
        monkeypatch.setenv(gen._env_key("VERSION_", tag), "1.8.1")
    published = tmp_path / "published"
    monkeypatch.setattr(gen, "_http_get", FakeYostar())
    assert (
        gen.main(["--out", str(tmp_path / "first"), "--merge-into", str(published)]) == 0
    )
    before_index = json.loads((published / "index.json").read_text())
    before_azupro = (published / "azurpromilia-jp-yostar.json").read_bytes()
    before_media = (published / "azurpromilia-jp-yostar.media.json").read_bytes()

    monkeypatch.setattr(gen, "_http_get", FakeYostar(fail={"AZUPRO_JP"}))
    assert (
        gen.main(["--out", str(tmp_path / "second"), "--merge-into", str(published)]) == 0
    )
    after_index = json.loads((published / "index.json").read_text())
    assert after_index["azurpromilia-jp-yostar"] == before_index["azurpromilia-jp-yostar"]
    assert (
        after_index["azurpromilia-jp-yostar"]["media"]
        == "azurpromilia-jp-yostar.media.json"
    )
    assert (published / "azurpromilia-jp-yostar.json").read_bytes() == before_azupro
    assert (published / "azurpromilia-jp-yostar.media.json").read_bytes() == before_media
    assert "azurpromilia-jp-yostar: not refreshed this run, keeping the published entry" in (
        capsys.readouterr().err
    )


def test_the_package_hosts_are_trusted_yostar_cdns():
    for url in (
        HOST + "/prod/AZUPRO_JP/AzurPromiliaJP.exe",
        HOST + "/zip_online_config_json/1.0.1drnTthGS.json",
        BACKUP_HOST + "/prod/AZUPRO_JP/AzurPromiliaJP.exe",
        "https://webusstatic.yo-star.com/art/cover.png",
        API_HOST + "/api/launcher/game/config",
    ):
        assert yostar.base_url_trusted(url), url
    for host in (
        "launcher-pkg-azupro-jp.yo-star.com",
        "launcher-pkg-azupro-jp-bk.yo-star.com",
        "webusstatic.yo-star.com",
        "api-launcher-jp.yo-star.com",
    ):
        assert yostar.host_allowed(host, None), host
    assert not yostar.host_allowed("launcher-pkg-azupro-jp.yo-star.com.evil.example", None)
    assert not yostar.host_allowed("evil-yo-star.com", None)


def test_azupro_plan_host_size_and_update(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()))
    game = backend(tmp_path, {"manifest_file": str(path)})
    plan = game.plan()
    assert plan.game_tag == "AZUPRO_JP"
    assert plan.exe == "AzurPromiliaJP.exe"
    assert len(plan.files) == 2
    assert sum(f.size for f in plan.files) == 5
    assert plan.files[0].url == HOST + "/prod/AZUPRO_JP/AzurPromiliaJP.exe"
    assert plan.files[0].crc64 == yostar.crc64(b"MZ")
    target = tmp_path / "installed"
    target.mkdir()
    (target / "AzurPromiliaJP.exe").write_bytes(b"MZ")
    missing, archives = directdl.plan_missing(plan, target, verify=True)
    assert [f.path for f in missing] == ["AzurPromiliaJP_Data/app.info"]
    assert archives == []
    newer = manifest()
    newer["version"] = "1.0.2"
    path.write_text(json.dumps(newer))
    assert game.plan().version == "1.0.2"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://evil.example/prod/AZUPRO_JP",
        "https://evil-yo-star.com/prod/AZUPRO_JP",
        "http://launcher-pkg-azupro-jp.yo-star.com/prod/AZUPRO_JP",
    ],
)
def test_azupro_plan_refuses_untrusted_download_hosts(tmp_path, base_url):
    payload = manifest()
    payload["base_url"] = base_url
    path = tmp_path / "bad-host.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(BackendError, match="invalid or incomplete"):
        backend(tmp_path, {"manifest_file": str(path)}).plan()


def test_azupro_redirects_stay_on_the_yostar_suffix(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()))
    guard = backend(tmp_path, {"manifest_file": str(path)}).plan().redirect_allowed
    assert guard is not None
    assert guard(HOST + "/prod/AZUPRO_JP/AzurPromiliaJP.exe")
    assert guard(BACKUP_HOST + "/prod/AZUPRO_JP/AzurPromiliaJP.exe")
    assert guard(API_HOST + "/api/launcher/game/config")
    assert not guard("https://evil.example/AzurPromiliaJP.exe")
    assert not guard("https://evil-yo-star.com/AzurPromiliaJP.exe")


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
def test_malformed_azupro_refused(tmp_path, change):
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
            BackendError, match=r"Azur Promilia \(JP\).*published manifest index.*turned off"
        ):
            action()
    with pytest.raises(
        BackendError, match=r"Azur Promilia \(JP\).*published manifest index.*turned off"
    ):
        game.plan()
    assert INSTALLED_PREFIX_KEY not in game.ctx.profile
    assert not game.install_dir().exists()
    with monkeypatch.context() as quiet:
        quiet.setattr(yostar, "load_manifest", lambda *a, **kw: pytest.fail("update fetch"))
        quiet.setattr(directdl, "sync", lambda *a, **kw: pytest.fail("update sync"))
        assert game.check_update() is None
    write_exe(game.install_dir() / game.direct_exe, "MZ")
    monkeypatch.setattr(type(game), "_ensure_default_runner", lambda *a, **kw: None)
    launched: list[str] = []
    monkeypatch.setattr(
        type(game), "_launch_direct", lambda self: launched.append("direct") or 0
    )
    assert game.launch() == 0
    assert launched == ["direct"]
    assert isinstance(game, ArknightsBackend)
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"Arknights_EN": {"url": "Arknights_EN.json"}}))
    game.ctx.profile["manifest_file"] = str(path)
    with pytest.raises(BackendError, match="invalid or incomplete"):
        game.plan()


def test_publisher_keeps_azupro_and_existing_filenames(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "azupro_publisher", ROOT / "scripts/publish_index.py"
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
                "SALT_AZUPRO_JP": SALT,
                "VERSION_AZUPRO_JP": "1.8.1",
            },
            "tags": ("StellaSora_EN", "Arknights_EN", "AZUPRO_JP"),
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
        assert set(index) == {
            "StellaSora_EN",
            "Arknights_EN",
            "azurpromilia-jp-yostar",
        }
        assert index["Arknights_EN"]["url"] == "Arknights_EN.json"
        assert index["StellaSora_EN"]["url"] == "StellaSora_EN.json"
        assert index["azurpromilia-jp-yostar"]["url"] == "azurpromilia-jp-yostar.json"
        assert (
            index["azurpromilia-jp-yostar"]["media"]
            == "azurpromilia-jp-yostar.media.json"
        )
        for tag in ("Arknights_EN", "StellaSora_EN", "azurpromilia-jp-yostar"):
            assert (out / f"{tag}.json").is_file()
            assert (out / f"{tag}.media.json").is_file()
        before = (out / "azurpromilia-jp-yostar.json").read_bytes()

        # A run without SALT_AZUPRO_JP publishes no Azur Promilia and keeps the existing entry.
        state["env"] = {"SALT_STELLASORA_EN": SALT, "SALT_ARKNIGHTS_EN": SALT}
        state["tags"] = ("StellaSora_EN", "Arknights_EN")
        assert module.publish_yostar(out, state["env"]) is False
        kept = json.loads((out / "index.json").read_text())
        assert kept["azurpromilia-jp-yostar"] == index["azurpromilia-jp-yostar"]
        assert (out / "azurpromilia-jp-yostar.json").read_bytes() == before
        assert (out / "azurpromilia-jp-yostar.media.json").is_file()
    finally:
        sys.modules.pop(spec.name, None)


def test_azupro_install_and_update_through_the_real_sync(tmp_path, monkeypatch):
    from nadare.backends.winegame import WineGameBackend

    monkeypatch.setattr(WineGameBackend, "_ensure_default_runner", lambda *a, **kw: None)
    monkeypatch.setattr(WineGameBackend, "_ensure_direct_prefix", lambda *a: None)
    contents = {
        "AzurPromiliaJP.exe": b"MZ",
        "AzurPromiliaJP_Data/app.info": b"azu",
    }
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
        relative = url.removeprefix(HOST + "/prod/AZUPRO_JP/")
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
        HOST + "/prod/AZUPRO_JP/AzurPromiliaJP.exe",
        HOST + "/prod/AZUPRO_JP/AzurPromiliaJP_Data/app.info",
    ]
    info = game.install_dir() / "AzurPromiliaJP_Data/app.info"
    assert info.read_bytes() == b"azu"

    requests.clear()
    info.write_bytes(b"old")  # same size, wrong CRC-64: only this file is re-fetched
    payload["version"] = "1.0.2"
    path.write_text(json.dumps(payload))
    game.update()
    assert requests == [HOST + "/prod/AZUPRO_JP/AzurPromiliaJP_Data/app.info"]
    assert info.read_bytes() == b"azu"
    assert game._read_version_file()["version"] == "1.0.2"
