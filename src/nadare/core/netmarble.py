# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Netmarble account login for Mongil: Star Dive.

The game wants a *launcher token* (a JWT) handed over by Netmarble's launcher. The
public sign-in page at ``members.netmarble.com/auth`` produces exactly that: with
``supportFeature=redirectParams|accessToken`` it appends ``accessToken=<jwt>`` to the
``redirectUrl`` and navigates there. Nadare runs a one-shot HTTP server on
``127.0.0.1`` for that redirect, so the user signs in in their own browser and no
password ever passes through Nadare.

The game is then started with ``NMAUTH_TYPE=netmarble`` and ``NMAUTH_TOKEN=<jwt>``
(as arguments) and a device key in the Wine registry that
matches the one used at sign-in.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from base64 import urlsafe_b64decode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from urllib.parse import parse_qs, parse_qsl, quote, urlsplit

from .events import Cancel

CLIENT_ID = "mq5RG0PGw6ipw35A"
GAME_CODE = "monster2"
LAUNCHER_CH = "ypWjRL2aNi"
LAUNCHER_VER = "1.7.0"
API_BASE = "https://apis.netmarble.com/cpplauncher/api/external"
AUTH_BASE = "https://members.netmarble.com/auth"
REDIRECT_PATH = "/redirectLauncher"
DEFAULT_PORT = 55000
#: Seconds before a token's expiry at which it already counts as expired.
EXPIRY_SKEW = 300

Fetch = Callable[[str, Mapping[str, str] | None], bytes]


class NetmarbleError(RuntimeError):
    """A sign-in step failed (bad token, timeout, unsafe input)."""


@dataclass(frozen=True)
class Session:
    token: str
    expires_at: int
    player_id: str
    device_key: str
    name: str = ""


def new_device_key() -> str:
    """A fresh random launcher device key (a UUID4 string)."""
    return str(uuid.uuid4())


def _check_device_key(device_key: str) -> None:
    if not re.fullmatch(r"[0-9A-Za-z-]{8,64}", device_key):
        raise NetmarbleError("invalid device key")


def check_state(state: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", state):
        raise NetmarbleError("invalid sign-in state")
    return state


def auth_url(
    port: int, device_key: str, language: str = "en", channel: str = "", state: str = ""
) -> str:
    """The sign-in URL to open in the browser. ``channel`` may be ``google``/``apple``
    to jump straight to that provider. ``state`` is a random value carried inside the
    redirect URL (the page keeps its query and appends the token), so a web page that
    merely guesses the port cannot plant a token; pair it with ``CallbackServer(state=)``.
    Raises :class:`NetmarbleError` for a device key that is not plain hex/dashes."""
    _check_device_key(device_key)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise NetmarbleError("invalid callback port")
    if language not in {"en", "ko", "ja", "zh", "es"}:
        language = "en"
    target = f"http://127.0.0.1:{port}{REDIRECT_PATH}"
    if state:
        target += f"?state={check_state(state)}"
    redirect = quote(target, safe="")
    url = (
        f"{AUTH_BASE}?clientId={CLIENT_ID}&countryCode=US&language={language}"
        f"&osType=PC&webViewType=web&authType=signin"
        f"&deviceKey={device_key}_monster2&showImageBanner=N&gameCode={GAME_CODE}"
        f"&forceCheckIdentity=N&redirectUrl={redirect}"
        "&supportFeature=redirectParams%7CaccessToken"
    )
    if channel in {"google", "apple"}:
        url += f"&idpType={channel}"
    return url


def decode_claims(token: str) -> tuple[str, int]:
    """``(sub, exp)`` from an (unverified) JWT's payload; ``("", 0)`` if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(urlsafe_b64decode(payload))
        if not isinstance(data, dict):
            return "", 0
        sub, exp = data.get("sub"), data.get("exp")
        return (str(sub) if sub is not None else "", int(exp) if exp is not None else 0)
    except Exception:
        return "", 0


def looks_like_token(text: str) -> bool:
    """True for a JWT-shaped string (three base64url parts, sane length)."""
    if not isinstance(text, str) or not 20 <= len(text) <= 8192:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*", text))


_TOKEN_KEYS = ("accessToken", "access_token", "launcher_token", "launcherToken", "token")


def _pick_token(values: Mapping[str, object]) -> str | None:
    for key in _TOKEN_KEYS:
        value = values.get(key)
        if isinstance(value, str) and looks_like_token(value):
            return value
    return None


def extract_token(text: str) -> str | None:
    """Find a launcher token in a pasted redirect URL, query string, JSON or bare token."""
    try:
        text = text.strip()
        if not text:
            return None
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except Exception:
                data = None
            if isinstance(data, dict):
                token = _pick_token(data)
                if token:
                    return token
        elif "?" in text:
            query = text.split("?", 1)[1].split("#", 1)[0]
            token = _pick_token(dict(parse_qsl(query)))
            if token:
                return token
        elif "=" in text and not any(ch.isspace() for ch in text):
            token = _pick_token(dict(parse_qsl(text)))
            if token:
                return token
        if looks_like_token(text):
            return text
    except Exception:
        return None
    return None


def session_from_token(
    token: str, device_key: str, *, now: float | None = None, name: str = ""
) -> Session:
    """Build a :class:`Session`; raises :class:`NetmarbleError` if the token is not
    JWT-shaped or already expired (including :data:`EXPIRY_SKEW`)."""
    if not looks_like_token(token):
        raise NetmarbleError("that does not look like a Netmarble token")
    if now is None:
        now = time.time()
    sub, exp = decode_claims(token)
    expires_at = int(now) + 30 * 86400 if exp == 0 else exp
    if expires_at - EXPIRY_SKEW <= now:
        raise NetmarbleError("the sign-in has already expired")
    return Session(
        token=token, expires_at=expires_at, player_id=sub, device_key=device_key, name=name
    )


def is_expired(session: Session, now: float | None = None) -> bool:
    if now is None:
        now = time.time()
    return session.expires_at - EXPIRY_SKEW <= now


class CallbackServer:
    """One-shot ``127.0.0.1`` server for the sign-in redirect.

    Binds ``port`` (falling back to a free port when it is taken). Accepts exactly one
    ``GET /redirectLauncher?accessToken=<jwt>`` whose ``Host`` is the loopback address
    and whose token is JWT-shaped; everything else gets 403/404/400 and is ignored.
    """

    def __init__(self, port: int = DEFAULT_PORT, state: str = "") -> None:
        self._state = check_state(state) if state else ""
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._token: str | None = None
        self._closed = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            #: A stalled client (e.g. a browser tab left half-open) must not pin a
            #: server thread forever.
            timeout = 10

            def log_message(self, format: str, *args: object) -> None:
                pass

            def _reply(
                self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8"
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                host = self.headers.get("Host", "")
                if host not in {f"127.0.0.1:{owner.port}", f"localhost:{owner.port}"}:
                    self._reply(403, b"forbidden")
                    return
                parts = urlsplit(self.path)
                if parts.path != REDIRECT_PATH:
                    self._reply(404, b"not found")
                    return
                query = parse_qs(parts.query)
                # A URL can carry several accessToken values; parse_qs would take
                # the first, so an attacker-appended duplicate must not be allowed
                # to win over the one Netmarble generated.
                tokens = query.get("accessToken") or []
                if len(tokens) > 1:
                    self._reply(400, b"multiple accessToken values")
                    return
                # The real sign-in page round-trips ``state`` (confirmed live with a
                # Google sign-in, which sends no Referer), so it is required.
                if owner._state and not hmac.compare_digest(
                    (query.get("state") or [""])[0].encode(), owner._state.encode()
                ):
                    self._reply(403, b"forbidden")
                    return
                token = tokens[0] if tokens else ""
                if not looks_like_token(token):
                    self._reply(400, b"missing or invalid token")
                    return
                with owner._lock:
                    if owner._token is not None:
                        # One-shot: the first valid token stands for this sign-in.
                        already = True
                    else:
                        owner._token = token
                        owner._event.set()
                        already = False
                if already:
                    self._reply(409, b"already signed in")
                    return
                self._reply(
                    200,
                    b'<!doctype html><html><head><meta charset="utf-8"><title>Nadare</title></head>'
                    b"<body><h1>Netmarble sign-in complete</h1>"
                    b"<p>You can close this tab and return to Nadare.</p></body></html>",
                    "text/html; charset=utf-8",
                )

        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )
        self._thread.start()

    port: int

    def wait(self, timeout: float = 300.0, cancel: Cancel | None = None) -> str:
        """Block until a token arrives. Raises :class:`NetmarbleError` on timeout and
        :class:`~nadare.core.events.Cancelled` when ``cancel`` fires."""
        deadline = time.monotonic() + timeout
        while True:
            if cancel is not None:
                cancel.raise_if_cancelled()
            if self._event.wait(0.1):
                with self._lock:
                    return self._token or ""
            if time.monotonic() >= deadline:
                raise NetmarbleError("timed out waiting for the Netmarble sign-in")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> CallbackServer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def fetch_user_name(token: str, fetch: Fetch | None = None) -> str:
    """Best-effort display name (the account nickname); ``""`` on any failure."""
    try:
        if fetch is None:
            from .download import http_get

            def fetch(url, headers=None):
                return http_get(url, headers=headers)

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-NM-LAUNCHER-CH": LAUNCHER_CH,
            "X-NM-LAUNCHER-VER": LAUNCHER_VER,
        }
        body = fetch(f"{API_BASE}/netmarble-auth/user", headers)
        data = json.loads(body)
        if not isinstance(data, dict) or data.get("code") != 0:
            return ""
        payload = data.get("data")
        if not isinstance(payload, dict):
            return ""
        nickname = payload.get("nickname")
        if not isinstance(nickname, str) or not nickname.strip():
            return ""
        name = re.sub(r"[\x00-\x1f\x7f]", "", nickname).strip()
        return name[:64]
    except Exception:
        return ""


def launch_args(session: Session) -> list[str]:
    # NMAUTH_TOKEN is passed on argv because the game reads it from there; setting
    # only the environment is unconfirmed to work. It is therefore visible in
    # /proc/<pid>/cmdline to any same-user process for as long as the game runs.
    return [
        "NMAUTH_TYPE=netmarble",
        "-NMAUTH_TYPE=netmarble",
        f"NMAUTH_TOKEN={session.token}",
        f"-NMAUTH_TOKEN={session.token}",
        "NMENV=nmp",
        "-NMENV=nmp",
    ]


def launch_env(session: Session) -> dict[str, str]:
    return {
        "NMAUTH_TYPE": "netmarble",
        "NMAUTH_TOKEN": session.token,
        "NMDeviceKey": session.device_key,
        "DEVICE_KEY": session.device_key,
        "NMENV": "nmp",
    }


_REG_HEADER = "[Software\\\\Netmarble\\\\NetmarbleSDK]"
_VALUE_PREFIX = '"NMDeviceKey"='


def _line_ending(line: str) -> str:
    return line[len(line.rstrip("\r\n")) :]


def _drop_empty_test_home(prefix: Path) -> None:
    home = Path(os.environ.get("HOME", ""))
    if not os.environ.get("PYTEST_CURRENT_TEST") or home.name != "home" or home.parent != prefix:
        return
    try:
        home.rmdir()
    except OSError:
        pass


def _atomic_write(path: Path, text: str, mode: int) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".user.reg.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            fh.write(text)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ensure_wine_device_key(prefix: Path | str, device_key: str) -> bool:
    """Make ``HKCU\\Software\\Netmarble\\NetmarbleSDK`` ``NMDeviceKey`` equal
    ``device_key`` by editing the prefix's ``user.reg``. Returns True if the file
    changed. Does nothing (False) when ``user.reg`` does not exist yet."""
    _check_device_key(device_key)
    path = Path(prefix) / "user.reg"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    lines = text.splitlines(keepends=True)
    value = f'"NMDeviceKey"="{device_key}"'

    start = next((i for i, line in enumerate(lines) if line.startswith(_REG_HEADER)), None)
    if start is None:
        stamp = int(time.time())
        filetime = (stamp + 11644473600) * 10_000_000
        if not text.endswith("\n"):
            text += "\n"
        text += f'\n{_REG_HEADER} {stamp}\n#time={filetime:x}\n{value}\n'
    else:
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines)
        )
        found = next(
            (i for i in range(start + 1, end) if lines[i].lstrip().startswith(_VALUE_PREFIX)),
            None,
        )
        if found is not None:
            old = lines[found].rstrip("\r\n")
            if old == value:
                return False
            lines[found] = value + _line_ending(lines[found])
            text = "".join(lines)
        else:
            insert_at = start + 1
            if insert_at < len(lines) and lines[insert_at].startswith("#time="):
                insert_at += 1
            newline = _line_ending(lines[start]) or "\n"
            lines.insert(insert_at, value + newline)
            text = "".join(lines)

    mode = stat.S_IMODE(path.stat().st_mode)
    _atomic_write(path, text, mode)
    _drop_empty_test_home(path.parent)
    return True
