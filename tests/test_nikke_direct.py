# SPDX-License-Identifier: GPL-3.0-or-later
"""NIKKE installs its launcher by Nadare's own download: no Tencent installer runs (#949).

The launcher is published anonymously as a rail_setup repository. Nadare asks the public
GetVersion API for it, downloads every file itself (MD5-checked) into ``C:\\NIKKE\\Launcher`` and
runs nothing. Only when that fails does the old path (silent downloader, then the installer
window) run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe
from _rail_fixtures import Repo

from nadare.backends import arknights as ak
from nadare.backends import launcher
from nadare.backends import winegame as wg
from nadare.backends.launcher import NikkeBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import railrepo, sevenzip
from nadare.core.events import Cancel, Cancelled
from nadare.core.sources import nikke as source

CDN = "https://cloud.nikke-kr.com/PC/prod/rid.50-r.test"
CLIENT_CDN = "https://cloud.nikke-kr.com/PC/prod/rid.51-r.client"
LAUNCHER = {
    "nikke_launcher.exe": b"MZ" + b"launcher" * 50,
    "tpf_ui": None,
    "tpf_ui\\skin.png": b"\x89PNG" + b"p" * 30,
    "intl_service\\INTLRuntime.dll": b"MZdll" * 20,
}
CLIENT = {
    "NIKKE": None,
    "NIKKE\\game": None,
    "NIKKE\\game\\nikke.exe": b"MZ" + b"client" * 80,
    "vc2015-2022x64\\vcruntime140.dll": b"MZdll" * 12,
}


def _client_repo(**changes) -> Repo:
    return Repo(CLIENT, repository_id=51, cdn_root=CLIENT_CDN, **changes)


# ---- the source ------------------------------------------------------------------------------
def _api_reply(
    repo: Repo,
    client: Repo | None = None,
    *,
    client_changes: dict | None = None,
    **changes,
) -> bytes:
    changes_for_client = {"game_id": "16601", **(client_changes or {})}
    version_info = (
        client.version_info(**changes_for_client) if client is not None else {"game_id": "16601"}
    )
    return json.dumps(
        {
            "result": {"error_code": 0, "error_message": "success"},
            "version_info": version_info,
            "launcher_version_info": repo.version_info(**changes),
        }
    ).encode()


def test_the_api_request_is_the_anonymous_one_the_launcher_makes():
    seen = {}

    def post(url, body, headers):
        seen.update(url=url, body=json.loads(body), headers=dict(headers))
        return _api_reply(Repo(LAUNCHER, cdn_root=CDN))

    info = source.launcher_info(post=post)
    assert seen["url"] == "https://www.jupiterlauncher.com/api/v1/fleet.repo.game.RepoSVC/GetVersion"
    assert seen["body"] == {
        "game_id": 16601, "branch_id": 1, "version_id": "0", "channel_id": 1, "need_launcher": 1
    }
    assert seen["headers"]["Referer"] == "https://www.jupiterlauncher.com/"
    assert "Cookie" not in seen["headers"] and "Authorization" not in seen["headers"]
    assert info["game_id"] == "16701"


@pytest.mark.parametrize(
    "reply",
    [
        b"not json",
        json.dumps({"result": {"error_code": 5}}).encode(),
        json.dumps({"result": {"error_code": 0}}).encode(),  # no launcher record
    ],
)
def test_a_bad_api_reply_is_a_rail_error(reply):
    with pytest.raises(railrepo.RailError):
        source.launcher_info(post=lambda url, body, headers: reply)


@pytest.mark.parametrize(
    "changes", [{"game_id": "16601"}, {"branch_id": "2"}]
)
def test_only_the_global_launcher_is_accepted(changes):
    reply = _api_reply(Repo(LAUNCHER, cdn_root=CDN), **changes)
    with pytest.raises(railrepo.RailError):
        source.launcher_info(post=lambda url, body, headers: reply)


def test_the_game_client_record_comes_from_the_same_reply():
    client = _client_repo()
    seen = []
    reply = _api_reply(Repo(LAUNCHER, cdn_root=CDN), client)

    def post(url, body, headers):
        seen.append(json.loads(body))
        return reply

    info = source.client_info(post=post)
    assert info["game_id"] == "16601"
    assert info["branch_id"] == "1"
    assert seen == [
        {"game_id": 16601, "branch_id": 1, "version_id": "0", "channel_id": 1, "need_launcher": 1}
    ]


def test_the_game_client_record_retries_without_need_launcher():
    client = _client_repo()
    seen = []

    def post(url, body, headers):
        request = json.loads(body)
        seen.append(request["need_launcher"])
        return json.dumps(
            {
                "result": {"error_code": 0},
                "launcher_version_info": Repo(LAUNCHER, cdn_root=CDN).version_info(),
                **({"version_info": client.version_info(game_id="16601")}
                   if request["need_launcher"] == 0 else {}),
            }
        ).encode()

    info = source.client_info(post=post)
    assert info["game_id"] == "16601"
    assert seen == [1, 0]


def test_a_missing_game_client_record_is_a_rail_error_after_one_retry():
    seen = []

    def post(url, body, headers):
        seen.append(json.loads(body)["need_launcher"])
        return json.dumps({"result": {"error_code": 0}}).encode()

    with pytest.raises(railrepo.RailError):
        source.client_info(post=post)
    assert seen == [1, 0]


@pytest.mark.parametrize("changes", [{"game_id": "16701"}, {"branch_id": "2"}])
def test_only_the_global_game_client_is_accepted(changes):
    reply = _api_reply(
        Repo(LAUNCHER, cdn_root=CDN), _client_repo(), client_changes=changes
    )
    with pytest.raises(railrepo.RailError):
        source.client_info(post=lambda url, body, headers: reply)


@pytest.mark.parametrize(
    "cdn", ["http://cloud.nikke-kr.com/PC", "https://evil.example/PC", "https://cloud.nikke-kr.com.evil.example/PC"]
)
def test_repositories_must_come_from_the_publishers_cdn(cdn):
    info = Repo(LAUNCHER, cdn_root=cdn).version_info()
    with pytest.raises(railrepo.RailError):
        source.launcher_repositories(info)
    client_info = Repo(CLIENT, cdn_root=cdn).version_info(game_id="16601")
    with pytest.raises(railrepo.RailError):
        source.game_repositories(client_info)


def test_the_game_client_is_installed_into_the_install_root(tmp_path):
    client = _client_repo()
    result = source.install_game(
        tmp_path,
        post=lambda url, body, headers: _api_reply(Repo(LAUNCHER, cdn_root=CDN), client),
        fetch=client.fetch,
    )
    assert result.files == sum(1 for data in CLIENT.values() if data is not None)
    assert (tmp_path / "NIKKE" / "game" / "nikke.exe").read_bytes() == CLIENT[
        "NIKKE\\game\\nikke.exe"
    ]
    assert (tmp_path / "vc2015-2022x64" / "vcruntime140.dll").is_file()


def test_redirects_may_only_stay_on_the_cdn_over_https():
    assert source.cdn_url_allowed("https://cloud.nikke-kr.com/PC/x.wgc")
    assert not source.cdn_url_allowed("http://cloud.nikke-kr.com/PC/x.wgc")
    assert not source.cdn_url_allowed("https://elsewhere.example/x.wgc")


def test_fetch_object_refuses_other_hosts_without_a_request(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("no request may be made")

    monkeypatch.setattr(source.download, "http_open", boom)
    with pytest.raises(railrepo.RailError):
        source.fetch_object("https://evil.example/x", 4, "00" * 16)


# ---- the backend -----------------------------------------------------------------------------
@pytest.fixture
def rig(tmp_path, monkeypatch):
    """The CDN and the old install path are faked; ``ran`` records programs started."""
    repo = Repo(LAUNCHER, cdn_root=CDN)
    client = _client_repo()
    ran: list[list[str]] = []
    state = {
        "direct_fails": False,
        "client_fails": False,
        "downloaded_installer": False,
        "client": client,
    }

    def fake_info(post=None):
        if state["direct_fails"]:
            raise railrepo.RailError("GetVersion said no")
        return repo.version_info()

    def fake_client_info(post=None):
        if state["client_fails"]:
            raise railrepo.RailError("GetVersion said no client")
        return client.version_info(game_id="16601")

    def fake_fetch(url, size, md5_hex):
        if url in repo.cdn:
            return repo.fetch(url, size, md5_hex)
        return client.fetch(url, size, md5_hex)

    def fake_stream(argv, env, on_line=None, cancel=None):
        ran.append(list(argv))
        write_exe(
            tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ"
        )
        return 0

    def fake_download_installer(self, url):
        state["downloaded_installer"] = True
        return write_exe(tmp_path / "nikke-wrapper.exe", "MZ")

    def fake_extract(sevenz, archive, outdir, run=None):
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "nikkeminiloader.exe").write_text("MZ")

    monkeypatch.setattr(source, "launcher_info", fake_info)
    monkeypatch.setattr(source, "client_info", fake_client_info)
    monkeypatch.setattr(source, "fetch_object", fake_fetch)
    monkeypatch.setattr(ak, "stream", fake_stream)
    monkeypatch.setattr(wg, "stream", fake_stream)
    monkeypatch.setattr(WineGameBackend, "_ensure_prefix", lambda self: None)
    monkeypatch.setattr(WineGameBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(WineGameBackend, "_download_installer", fake_download_installer)
    monkeypatch.setattr(launcher, "_extract_nsis", fake_extract)
    monkeypatch.setattr(sevenzip, "find", lambda **kw: "/usr/bin/7z")
    monkeypatch.delenv("NADARE_NIKKE_DIRECT", raising=False)
    monkeypatch.delenv("NADARE_NIKKE_SILENT", raising=False)
    (tmp_path / "prefix" / "drive_c").mkdir(parents=True)
    return repo, ran, state


def _backend(tmp_path: Path, events: list | None = None, **profile) -> NikkeBackend:
    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    ctx = make_context(tmp_path, "launcher-nikke", values)
    if events is not None:
        ctx.emit = events.append
    return NikkeBackend(ctx)


def test_install_downloads_the_launcher_and_game_client_and_runs_nothing(tmp_path, rig):
    _repo, ran, state = rig
    events: list = []
    backend = _backend(tmp_path, events)
    backend.install()
    assert ran == [] and not state["downloaded_installer"]
    root = tmp_path / "prefix" / "drive_c" / "NIKKE"
    launcher_dir = root / "Launcher"
    assert (launcher_dir / "nikke_launcher.exe").read_bytes() == LAUNCHER["nikke_launcher.exe"]
    assert (launcher_dir / "intl_service" / "INTLRuntime.dll").is_file()
    assert (root / "NIKKE" / "game" / "nikke.exe").read_bytes() == CLIENT[
        "NIKKE\\game\\nikke.exe"
    ]
    assert backend.find_launcher() == launcher_dir / "nikke_launcher.exe"
    steps = [e.message for e in events if e.kind == "step"]
    assert any("game client" in message and "1.1 GB" in message for message in steps)
    done = [e.message for e in events if e.kind == "done"]
    assert done and "Play" in done[-1]
    assert any(e.kind == "progress" for e in events)


def test_a_failed_direct_download_falls_back_to_the_old_path_and_says_why(tmp_path, rig):
    _repo, ran, state = rig
    state["direct_fails"] = True
    events: list = []
    backend = _backend(tmp_path, events)
    backend.install()
    assert state["downloaded_installer"] and len(ran) == 1
    notes = " ".join(e.message for e in events if e.message)
    assert "GetVersion said no" in notes


def test_a_failed_client_download_falls_back_to_the_old_path_and_says_why(tmp_path, rig):
    _repo, ran, state = rig
    state["client_fails"] = True
    events: list = []
    backend = _backend(tmp_path, events)
    backend.install()
    assert state["downloaded_installer"] and len(ran) == 1
    notes = " ".join(e.message for e in events if e.message)
    assert "GetVersion said no client" in notes


def test_the_switch_turns_the_direct_download_off(tmp_path, rig, monkeypatch):
    _repo, ran, state = rig
    monkeypatch.setenv("NADARE_NIKKE_DIRECT", "0")
    _backend(tmp_path).install()
    assert state["downloaded_installer"] and len(ran) == 1


def test_installer_arguments_mean_the_installer_is_wanted(tmp_path, rig):
    _repo, ran, state = rig
    _backend(tmp_path, installer_args="/S").install()
    assert state["downloaded_installer"]


def test_cancelling_the_direct_download_cancels_the_install(tmp_path, rig, monkeypatch):
    repo, ran, state = rig
    backend = _backend(tmp_path)

    def cancelling(url, size, md5_hex):
        if url.endswith(".wgc"):
            backend.ctx.cancel.cancel()
        return repo.fetch(url, size, md5_hex)

    monkeypatch.setattr(source, "fetch_object", cancelling)
    with pytest.raises(Cancelled):
        backend.install()
    assert ran == [] and not state["downloaded_installer"]


def test_cancelling_the_client_download_cancels_the_install(tmp_path, rig, monkeypatch):
    repo, ran, state = rig
    client = state["client"]
    backend = _backend(tmp_path)
    cancelled = False

    def cancelling(url, size, md5_hex):
        nonlocal cancelled
        if not cancelled and url.startswith(CLIENT_CDN) and url.endswith(".wgc"):
            cancelled = True
            backend.ctx.cancel.cancel()
        if url in repo.cdn:
            return repo.fetch(url, size, md5_hex)
        return client.fetch(url, size, md5_hex)

    monkeypatch.setattr(source, "fetch_object", cancelling)
    with pytest.raises(Cancelled):
        backend.install()
    assert cancelled
    assert ran == [] and not state["downloaded_installer"]


def test_a_launcher_folder_outside_the_prefix_still_lands_in_c_nikke(tmp_path, rig):
    """Add game gives every profile an install_dir outside its prefix; launchers ignore it."""
    _repo, ran, _state = rig
    backend = _backend(tmp_path, install_dir=str(tmp_path / "games" / "nikke"))
    backend.install()
    assert (tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe").is_file()


def test_a_reinstall_keeps_the_verified_files(tmp_path, rig, monkeypatch):
    repo, ran, state = rig
    client = state["client"]
    _backend(tmp_path).install()
    fetched: list[str] = []

    def counting(url, size, md5_hex):
        fetched.append(url)
        if url in repo.cdn:
            return repo.fetch(url, size, md5_hex)
        return client.fetch(url, size, md5_hex)

    monkeypatch.setattr(source, "fetch_object", counting)
    _backend(tmp_path).install()
    assert all(not u.endswith(".wgc") for u in fetched)
    root = tmp_path / "prefix" / "drive_c" / "NIKKE"
    assert (root / "NIKKE" / "game" / "nikke.exe").is_file()


def test_cancel_object_is_the_backends_own(tmp_path, rig):
    backend = _backend(tmp_path)
    assert isinstance(backend.ctx.cancel, Cancel)
