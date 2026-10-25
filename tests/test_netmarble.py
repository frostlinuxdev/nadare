# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for nadare.core.netmarble: sign-in URL, token parsing, callback server, registry."""

from __future__ import annotations

import base64
import http.client
import json
import socket
import threading
import time
import urllib.parse
import urllib.request

import pytest

from nadare.core import netmarble as nm
from nadare.core.events import Cancel, Cancelled


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(sub: str = "P12345", exp: int | None = None) -> str:
    exp = int(time.time()) + 86400 if exp is None else exp
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"sub": sub, "exp": exp}).encode())
    return f"{header}.{payload}.{b64(b'signature-bytes')}"


DK = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"


# ------------------------------------------------------------------ device key / url
def test_new_device_key_is_a_uuid4_string():
    a, b = nm.new_device_key(), nm.new_device_key()
    assert a != b and len(a) == 36 and a.count("-") == 4


def test_auth_url_parameters():
    url = nm.auth_url(55000, DK, "en")
    parts = urllib.parse.urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://members.netmarble.com/auth"
    q = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    assert q["clientId"] == nm.CLIENT_ID and q["gameCode"] == "monster2"
    assert q["deviceKey"] == f"{DK}_monster2"
    assert q["redirectUrl"] == "http://127.0.0.1:55000/redirectLauncher"
    assert q["supportFeature"] == "redirectParams|accessToken"
    assert q["osType"] == "PC" and q["webViewType"] == "web" and q["authType"] == "signin"
    assert "idpType" not in q
    assert "idpType=google" in nm.auth_url(1, DK, "en", "google")
    assert "idpType=apple" in nm.auth_url(1, DK, "en", "apple")


def test_auth_url_languages_and_unsafe_device_keys():
    assert "language=ko" in nm.auth_url(1, DK, "ko")
    assert "language=en" in nm.auth_url(1, DK, "xx")  # unknown -> English
    for bad in ("", "x y", "a&b=c", "../x", "k" * 100, "a\nb"):
        with pytest.raises(nm.NetmarbleError):
            nm.auth_url(1, bad)


# ------------------------------------------------------------------ tokens
def test_decode_claims_and_garbage():
    assert nm.decode_claims(make_jwt("P9", 1234567890)) == ("P9", 1234567890)
    for junk in ("", "abc", "a.b", "a.!!!.c", f"x.{b64(b'not json')}.y"):
        assert nm.decode_claims(junk) == ("", 0)


def test_looks_like_token():
    assert nm.looks_like_token(make_jwt())
    for bad in ("", "short", "a.b", "has space.in.it", "x" * 9000, "a.b.c" + "\n", "/redirectLauncher"):
        assert not nm.looks_like_token(bad)


@pytest.mark.parametrize(
    "wrap",
    [
        lambda t: f"http://127.0.0.1:55000/redirectLauncher?accessToken={t}",
        lambda t: f"http://127.0.0.1:55000/redirectLauncher?netmarbleId=1&accessToken={t}&refreshToken=zz",
        lambda t: f"nmmonster2://x?access_token={t}#frag",
        lambda t: f"accessToken={t}",
        lambda t: json.dumps({"accessToken": t, "refreshToken": "r"}),
        lambda t: json.dumps({"launcher_token": t}),
        lambda t: f"  {t}\n",
        lambda t: t,
    ],
)
def test_extract_token_accepts_the_usual_forms(wrap):
    t = make_jwt()
    assert nm.extract_token(wrap(t)) == t


@pytest.mark.parametrize(
    "junk",
    ["", "hello", "/redirectLauncher", "http://x/y?accessToken=short", '{"accessToken": 5}', "a b c" * 10],
)
def test_extract_token_rejects_junk(junk):
    assert nm.extract_token(junk) is None


def test_session_from_token_and_expiry():
    now = 1_800_000_000
    t = make_jwt("P1", now + 3600)
    s = nm.session_from_token(t, DK, now=now, name="Cat")
    assert (s.token, s.expires_at, s.player_id, s.device_key, s.name) == (t, now + 3600, "P1", DK, "Cat")
    assert not nm.is_expired(s, now) and nm.is_expired(s, now + 3600 - 100)  # inside the skew
    with pytest.raises(nm.NetmarbleError):
        nm.session_from_token(make_jwt(exp=now - 5), DK, now=now)
    with pytest.raises(nm.NetmarbleError):
        nm.session_from_token("not-a-token", DK, now=now)
    # no exp claim at all: accepted, treated as not expiring soon
    nexp = f"{b64(b'{}')}.{b64(json.dumps({'sub': 'P'}).encode())}.{b64(b'sig-sig-sig')}"
    assert nm.session_from_token(nexp, DK, now=now).expires_at > now


# ------------------------------------------------------------------ callback server
def get(port: int, path: str, host: str | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{port}")
        conn.endheaders()
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def test_server_receives_the_redirect_token():
    token = make_jwt()
    with nm.CallbackServer(0) as srv:
        out: dict[str, str] = {}
        t = threading.Thread(target=lambda: out.setdefault("tok", srv.wait(5)))
        t.start()
        status, body = get(srv.port, f"/redirectLauncher?accessToken={token}&x=1")
        t.join(5)
    assert status == 200 and b"<html" in body.lower()
    assert out["tok"] == token


def test_server_ignores_everything_that_is_not_the_redirect():
    token = make_jwt()
    with nm.CallbackServer(0) as srv:
        assert get(srv.port, f"/other?accessToken={token}")[0] == 404
        assert get(srv.port, "/favicon.ico")[0] == 404
        assert get(srv.port, "/redirectLauncher")[0] in (400, 404)
        assert get(srv.port, "/redirectLauncher?accessToken=short")[0] == 400
        assert get(srv.port, f"/redirectLauncher?accessToken={token}", host="evil.example")[0] == 403
        assert get(srv.port, f"/redirectLauncher?accessToken={token}", host="127.0.0.1:1")[0] == 403
        with pytest.raises(nm.NetmarbleError):
            srv.wait(0.3)  # none of the above was accepted
        # the real one is still accepted afterwards
        assert get(srv.port, f"/redirectLauncher?accessToken={token}")[0] == 200
        assert srv.wait(2) == token


def test_server_accepts_localhost_as_host_header():
    token = make_jwt()
    with nm.CallbackServer(0) as srv:
        status, _ = get(srv.port, f"/redirectLauncher?accessToken={token}", host=f"localhost:{srv.port}")
        assert status == 200 and srv.wait(2) == token


def test_server_takes_only_the_first_token():
    first, second = make_jwt("A"), make_jwt("B")
    with nm.CallbackServer(0) as srv:
        assert get(srv.port, f"/redirectLauncher?accessToken={first}")[0] == 200
        try:
            get(srv.port, f"/redirectLauncher?accessToken={second}")
        except OSError:
            pass
        assert srv.wait(2) == first


def test_server_rejects_a_second_valid_token_without_replacing_the_first():
    first, second = make_jwt("A"), make_jwt("B")
    with nm.CallbackServer(0) as srv:
        assert get(srv.port, f"/redirectLauncher?accessToken={first}")[0] == 200
        assert get(srv.port, f"/redirectLauncher?accessToken={second}")[0] == 409
        assert srv.wait(2) == first


def test_server_rejects_duplicate_access_tokens():
    token, other = make_jwt("A"), make_jwt("B")
    with nm.CallbackServer(0) as srv:
        status, _ = get(
            srv.port,
            f"/redirectLauncher?accessToken={token}&accessToken={other}",
        )
        assert status == 400
        with pytest.raises(nm.NetmarbleError):
            srv.wait(0.3)
        # A single token is still accepted afterwards.
        assert get(srv.port, f"/redirectLauncher?accessToken={token}")[0] == 200
        assert srv.wait(2) == token


def test_server_has_a_timeout_and_drops_a_silent_connection():
    with nm.CallbackServer(0) as srv:
        handler = srv._httpd.RequestHandlerClass
        assert handler.timeout is not None
        old_timeout = handler.timeout
        handler.timeout = 0.5
        try:
            sock = socket.create_connection(("127.0.0.1", srv.port), timeout=3)
            try:
                # Send nothing: the handler must drop us on its own timeout.
                assert sock.recv(1) == b""
            finally:
                sock.close()
        finally:
            handler.timeout = old_timeout


def test_server_timeout_and_cancel():
    with nm.CallbackServer(0) as srv:
        t0 = time.time()
        with pytest.raises(nm.NetmarbleError, match="(?i)time"):
            srv.wait(0.3)
        assert time.time() - t0 < 3
        cancel = Cancel()
        threading.Timer(0.2, cancel.cancel).start()
        with pytest.raises(Cancelled):
            srv.wait(10, cancel)


def test_server_falls_back_when_the_port_is_busy_and_binds_loopback_only():
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        with nm.CallbackServer(busy.getsockname()[1]) as srv:
            assert srv.port != busy.getsockname()[1]
            assert get(srv.port, "/x")[0] == 404
            for s in (srv,):
                assert s._httpd.server_address[0] == "127.0.0.1"  # never 0.0.0.0
    finally:
        busy.close()


def test_server_is_closed_after_the_context():
    with nm.CallbackServer(0) as srv:
        port = srv.port
    with pytest.raises(OSError):
        get(port, "/redirectLauncher")


# ------------------------------------------------------------------ user name
def test_fetch_user_name():
    token = make_jwt()
    calls = []

    def fake(url, headers=None):
        calls.append((url, dict(headers or {})))
        return json.dumps({"code": 0, "data": {"nickname": "Mongil Fan", "mailAddress": "a@b.c"}}).encode()

    assert nm.fetch_user_name(token, fake) == "Mongil Fan"
    url, headers = calls[0]
    assert url == f"{nm.API_BASE}/netmarble-auth/user"
    assert headers["Authorization"] == f"Bearer {token}" and headers["X-NM-LAUNCHER-CH"] == nm.LAUNCHER_CH


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"code": 1, "msg": "no"}', b'{"code": 0, "data": null}', b'{"code": 0, "data": {"mailAddress": "x@y.z"}}'],
)
def test_fetch_user_name_never_raises_and_never_leaks_the_email(body):
    assert nm.fetch_user_name(make_jwt(), lambda u, h=None: body) == ""


def test_fetch_user_name_survives_a_network_error():
    def boom(url, headers=None):
        raise OSError("down")

    assert nm.fetch_user_name(make_jwt(), boom) == ""


# ------------------------------------------------------------------ launch extras
def test_launch_args_and_env():
    s = nm.session_from_token(make_jwt(), DK, name="x")
    assert nm.launch_args(s) == [
        "NMAUTH_TYPE=netmarble",
        "-NMAUTH_TYPE=netmarble",
        f"NMAUTH_TOKEN={s.token}",
        f"-NMAUTH_TOKEN={s.token}",
        "NMENV=nmp",
        "-NMENV=nmp",
    ]
    assert nm.launch_env(s) == {
        "NMAUTH_TYPE": "netmarble",
        "NMAUTH_TOKEN": s.token,
        "NMDeviceKey": DK,
        "DEVICE_KEY": DK,
        "NMENV": "nmp",
    }


# ------------------------------------------------------------------ wine registry
REG = (
    "WINE REGISTRY Version 2\n;; All keys relative to \\\\User\\\\S-1-5-21-0-0-0-1000\n\n#arch=win64\n\n"
    "[Software\\\\Wine\\\\Drivers] 1700000000\n#time=1d0000000000000\n\"Audio\"=\"alsa\"\n\n"
    "{netmarble}"
    "[Software\\\\Zeta] 1700000001\n#time=1d0000000000001\n\"k\"=\"v\"\n"
)
SECTION = "[Software\\\\Netmarble\\\\NetmarbleSDK]"


def write_reg(prefix, netmarble=""):
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / "user.reg").write_text(REG.replace("{netmarble}", netmarble), encoding="utf-8")
    return prefix / "user.reg"


def test_registry_adds_a_missing_section(tmp_path):
    reg = write_reg(tmp_path)
    before = reg.read_text()
    assert nm.ensure_wine_device_key(tmp_path, DK) is True
    after = reg.read_text()
    assert SECTION in after and f'"NMDeviceKey"="{DK}"' in after
    assert before.rstrip("\n") in after  # nothing else changed
    assert after.count(SECTION) == 1


def test_registry_updates_an_existing_value(tmp_path):
    sec = f'{SECTION} 1700000002\n#time=1d0000000000002\n"NMDeviceKey"="old-key"\n"Other"="keep"\n\n'
    reg = write_reg(tmp_path, sec)
    assert nm.ensure_wine_device_key(tmp_path, DK) is True
    text = reg.read_text()
    assert f'"NMDeviceKey"="{DK}"' in text and "old-key" not in text and '"Other"="keep"' in text
    assert '"k"="v"' in text and '"Audio"="alsa"' in text


def test_registry_adds_the_value_to_an_existing_section(tmp_path):
    sec = f'{SECTION} 1700000002\n#time=1d0000000000002\n"Other"="keep"\n\n'
    reg = write_reg(tmp_path, sec)
    assert nm.ensure_wine_device_key(tmp_path, DK) is True
    text = reg.read_text()
    assert text.count("NMDeviceKey") == 1 and '"Other"="keep"' in text
    # the value landed inside the right section, not the next one
    inside = text.split(SECTION, 1)[1].split("\n[", 1)[0]
    assert f'"NMDeviceKey"="{DK}"' in inside


def test_registry_is_idempotent_and_leaves_no_temp_files(tmp_path):
    reg = write_reg(tmp_path)
    assert nm.ensure_wine_device_key(tmp_path, DK) is True
    once = reg.read_text()
    assert nm.ensure_wine_device_key(tmp_path, DK) is False
    assert reg.read_text() == once
    assert sorted(p.name for p in tmp_path.iterdir()) == ["user.reg"]


def test_registry_without_user_reg_is_a_noop(tmp_path):
    assert nm.ensure_wine_device_key(tmp_path, DK) is False
    assert not (tmp_path / "user.reg").exists()


@pytest.mark.parametrize("bad", ['x"\n[Evil]', "a b", "", "k" * 100, 'q"q'])
def test_registry_refuses_unsafe_device_keys(tmp_path, bad):
    reg = write_reg(tmp_path)
    before = reg.read_text()
    with pytest.raises(nm.NetmarbleError):
        nm.ensure_wine_device_key(tmp_path, bad)
    assert reg.read_text() == before


# ------------------------------------------------------------------ state (anti drive-by)
def test_auth_url_carries_the_state_inside_the_redirect_url():
    url = nm.auth_url(55000, DK, "en", state="abcDEF123_-xyz789")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["redirectUrl"] == "http://127.0.0.1:55000/redirectLauncher?state=abcDEF123_-xyz789"
    for bad in ("short", "has space!!", "a&b=c-long-enough", "x" * 80):
        with pytest.raises(nm.NetmarbleError):
            nm.auth_url(55000, DK, "en", state=bad)


def test_a_server_with_a_state_rejects_requests_that_lack_it():
    token = make_jwt()
    good = "s3cr3t-state-value1"
    with nm.CallbackServer(0, state=good) as srv:
        assert get(srv.port, f"/redirectLauncher?accessToken={token}")[0] == 403
        assert get(srv.port, f"/redirectLauncher?accessToken={token}&state=wrong-state-value-x")[0] == 403
        assert get(srv.port, f"/redirectLauncher?state={good}")[0] == 400  # right state, no token
        with pytest.raises(nm.NetmarbleError):
            srv.wait(0.3)
        assert get(srv.port, f"/redirectLauncher?state={good}&accessToken={token}")[0] == 200
        assert srv.wait(2) == token


def test_a_stateless_server_still_accepts_the_plain_redirect():
    token = make_jwt()
    with nm.CallbackServer(0) as srv:
        assert get(srv.port, f"/redirectLauncher?accessToken={token}")[0] == 200


def get_with_headers(port: int, path: str, headers: dict[str, str]) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        r = conn.getresponse()
        r.read()
        return r.status
    finally:
        conn.close()


def test_a_netmarble_referer_is_not_a_substitute_for_the_state():
    token = make_jwt()
    with nm.CallbackServer(0, state="s3cr3t-state-value1") as srv:
        path = f"/redirectLauncher?accessToken={token}"
        # the real redirect carries the state and no Referer, so a Referer proves nothing
        for referer in (
            "https://evil.example/page",
            "https://members.netmarble.com.evil.example/",
            "http://members.netmarble.com/",
            "https://members.netmarble.com/auth?x=1",
        ):
            assert get_with_headers(srv.port, path, {"Referer": referer}) == 403
        with pytest.raises(nm.NetmarbleError):
            srv.wait(0.2)
        assert get(srv.port, path + "&state=s3cr3t-state-value1")[0] == 200
        assert srv.wait(2) == token
