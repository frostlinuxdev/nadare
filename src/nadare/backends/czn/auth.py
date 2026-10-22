#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 czn-linux contributors
# Derived from yporoc/ChaosZeroNightmareLauncher-Lite (GPL-3.0), which
# specifies no "or later" grant, so this file stays GPL-3.0-only.
"""czn_auth.py — STOVE login + game-token exchange for Chaos Zero Nightmare,
native Linux, no STOVE launcher.

Ported from yporoc/ChaosZeroNightmareLauncher-Lite (GPL-3.0): the STOVE auth
protocol (QR login, launcher-token refresh, gc/check game-token exchange) is
its work; this is a standalone, GUI-less, Linux reimplementation that writes
``tools/session.json`` for czn_pcsdk_stub.c to serve.

Flow:
  1. login   — QR login (scan with the STOVE mobile app), once. Hands the
               rotating launcher refresh_token to the file named by
               CZN_CREDENTIAL_OUT / --credential-out (required).
  2. token   — refresh (provider_cd=RT) -> gc/v1.4/check -> fresh game-level
               access_token, then write session.json. Run this before each
               launch; no STOVE, no interaction, until the refresh_token is
               invalidated (code 44010 -> run `login` again).

STOVE's CDN filters by TLS/JA3, so curl_cffi's Chrome impersonation is required
(plain requests gets reset). Credentials live only in the 0600 credential sink and
session.json; neither is printed.

Machine interface (for launchers embedding this): add --json and every line on
stdout is one JSON object with an "event" key. The game/access token never
appears on it. The rotating launcher refresh token (the account credential) is
handed to the embedding backend through a 0600 file named by the
``CZN_CREDENTIAL_OUT`` environment variable, never on stdout or in argv.
  login   -> {"event":"qr","png":<path>,"url":<str>,"timeout":<s>}
             {"event":"qr_status","state":"init|ongoing|success|..."}  (on change)
             {"event":"logged_in"}  then the `token` events below
  token   -> {"event":"session","path":<path>,"expires_at":<unix s>,"expires_in":<s>}
  status  -> {"event":"status","logged_in":<bool>,"region":{"nation","timezone"},
              "session":{"path","expires_at","valid"}|null}
  failure -> {"event":"error","kind":<kind>,"message":<str>} and exit code:
             2 auth_failed, 3 relogin_required (run `login`), 4 network
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

try:
    from curl_cffi import requests as creq
    from curl_cffi.const import CurlHttpVersion
    from curl_cffi.requests.exceptions import RequestException
except Exception:  # pragma: no cover
    sys.exit("curl_cffi is required: `.venv/bin/pip install -r requirements.txt` (see README)")

JSON_MODE = False


def say(text: str) -> None:
    """Human output. In --json mode it goes to stderr so stdout stays parseable."""
    print(text, file=sys.stderr if JSON_MODE else sys.stdout, flush=True)


def event(name: str, **fields) -> None:
    if JSON_MODE:
        print(json.dumps({"event": name, **fields}), flush=True)


class ReloginRequired(RuntimeError):
    """The saved refresh token is no longer accepted; a new QR login is needed."""


#: STOVE's "the launcher session was invalidated" code. Every signed call can
#: return it, and the right answer is always "log in again" (exit 3), never a
#: generic failure (exit 2).
RELOGIN_CODE = "44010"


def _check_code(data: dict, what: str) -> None:
    """Raise unless the STOVE envelope ``code`` is success (``0``/absent).

    ``44010`` becomes :class:`ReloginRequired`; any other code is a plain
    ``RuntimeError``. STOVE sometimes sends the code as a string, so normalise.
    """
    code = str(data.get("code", "0"))
    if code in ("0", "None"):
        return
    if code == RELOGIN_CODE:
        raise ReloginRequired(
            "refresh_token invalid (44010) — the account "
            "was used elsewhere or the token is stale; "
            "run `login` again"
        )
    raise RuntimeError(f"[{what}] code={code} msg={data.get('message')}")


def _check_http(r, what: str) -> None:
    """Raise on a non-2xx HTTP status; the body is never included."""
    status = getattr(r, "status_code", 200)
    if isinstance(status, int) and status >= 400:
        raise RuntimeError(f"[{what}] HTTP {status}")


def _write_private(path: Path, text: str) -> None:
    """Atomically write a file that is 0600 from creation (no world-readable window)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)  # in case tmp pre-existed with wider permissions
    tmp.replace(path)


def _write_private_bytes(path: Path, data: bytes) -> None:
    """Atomically write binary data that is 0600 from creation (e.g. the QR png)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# ---- client constants (from Lite; override via env) --------------------------
API_BASE = os.environ.get("CZN_API_BASE", "https://s-api.onstove.com")
API = os.environ.get("CZN_API", "https://api.onstove.com")
CLIENT_ID = os.environ.get(
    "CZN_CLIENT_ID", "5faa0926311687ccc34a598d9640a48909a86a0e93afdf586ab088a3d01a93d3"
)
# The official launcher's version string (STOVE.exe build 737 = 3.2.30.737, current on
# 2026-10-12; the launcher's own dpms feed is game_id 65000). Bump it with STOVE.
CALLER_ID = os.environ.get("CZN_CALLER_ID", "STOVE_LAUNCHER_VER.3.2.30.737")
GAME_ID = os.environ.get("CZN_GAME_ID", "STOVE_CHAOSZERO")
MARKET_GAME_ID = os.environ.get("CZN_MARKET_GAME_ID", "com.smilegate.chaoszero.stove.pc")

# Region sent as gds_info. The official launcher reports the region it detects
# the player connecting from; the closest local equivalent is the system's
# timezone. Every field can be overridden with the CZN_* variables below.
_GDPR = {
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
    "IS",
    "LI",
    "NO",
    "GB",
    "CH",
}


def _system_timezone() -> str | None:
    tz = os.environ.get("TZ", "").lstrip(":")
    if not tz:
        try:
            tz = os.path.realpath("/etc/localtime").split("/zoneinfo/", 1)[1]
        except (OSError, IndexError):
            return None
    return tz or None


def _country_for(tz: str | None) -> str | None:
    if not tz:
        return None
    for tab in ("/usr/share/zoneinfo/zone1970.tab", "/usr/share/zoneinfo/zone.tab"):
        try:
            for line in open(tab, encoding="utf-8"):
                if line.startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 3 and cols[2] == tz:
                    return cols[0].split(",")[0]  # zone1970 may list several
        except OSError:
            continue
    return None


def _default_gds() -> dict:
    tz = _system_timezone()
    nation = _country_for(tz)
    offset = time.localtime().tm_gmtoff // 60
    if not nation:  # undetectable: the defaults the Lite launcher ships with
        tz, nation, offset = "Asia/Tokyo", "JP", 540
    lang = (os.environ.get("LANG") or "en")[:2].lower()
    return {
        "is_default": False,
        "nation": os.environ.get("CZN_NATION", nation),
        "regulation": os.environ.get("CZN_REGULATION", "GDPR" if nation in _GDPR else "ETC"),
        "timezone": os.environ.get("CZN_TIMEZONE", tz),
        "utc_offset": int(os.environ.get("CZN_UTC_OFFSET", offset)),
        "lang": os.environ.get("CZN_LANG", lang if lang.isalpha() and lang != "c" else "en"),
    }


GDS_INFO = _default_gds()


def _xdg_fallback(env: str, default: str) -> Path:
    """XDG base used only when ``nadare.core.paths`` cannot be imported.

    Mirrors core.paths so standalone runs honour ``XDG_DATA_HOME`` rather than
    hard-coding ``~/.local/share``.
    """
    return Path(os.environ.get(env) or os.path.expanduser(default)) / "nadare"


def _default_state_dir() -> Path:
    """Nadare's per-user data dir; CZN_STATE_DIR always wins (the backend sets it
    to the active profile's data dir)."""
    try:
        from nadare.core import paths

        return paths.data_dir() / "czn"
    except Exception:
        return _xdg_fallback("XDG_DATA_HOME", "~/.local/share") / "czn"


STATE_DIR = Path(os.environ.get("CZN_STATE_DIR", str(_default_state_dir())))
STATE_FILE = STATE_DIR / "czn_auth.json"
SESSION_JSON = STATE_DIR / "session.json"


def _default_qr_png() -> Path:
    """Where a standalone ``login`` writes its QR image (never the package dir)."""
    return STATE_DIR / "czn_qr.png"


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(STATE_FILE, json.dumps(state, separators=(",", ":")))


def _take_credential_in() -> str | None:
    """Read and delete the 0600 file named by ``CZN_CREDENTIAL_IN``.

    The embedding backend hands the refresh token over this way instead of via
    the environment, which any same-user process can read from
    ``/proc/<pid>/environ`` while the worker runs.
    """
    path = os.environ.get("CZN_CREDENTIAL_IN")
    if not path:
        return None
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        Path(path).unlink()
    except OSError:
        pass
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    token = data.get("refresh_token") if isinstance(data, dict) else None
    return str(token) if token else None


def _caller_detail(state: dict) -> str:
    """40-hex install fingerprint STOVE writes to HKCU\\SOFTWARE\\SGUP. We keep
    our own in the state file; the server's validation of it is unconfirmed, so
    game_check self-heals a 70702 by rotating it."""
    cd = state.get("caller_detail")
    if not cd:
        cd = secrets.token_hex(20)
        state["caller_detail"] = cd
        _save_state(state)
    return cd


class StoveAuth:
    def __init__(self, state: dict):
        self.state = state
        self.gds = dict(GDS_INFO)
        self.session_tid = str(uuid.uuid4())
        self.s = creq.Session(
            impersonate="chrome", default_headers=False, http_version=CurlHttpVersion.V1_1
        )
        try:
            self.s.headers.clear()
        except Exception:
            pass
        self.launcher_access = None
        # The launcher refresh token is a credential: it comes from the
        # embedding backend (keyring) via the environment, or transiently from
        # a legacy state file that the backend is migrating away from. It is
        # never written back to the plaintext state file.
        self.launcher_refresh = (
            _take_credential_in()
            or os.environ.get("CZN_REFRESH_TOKEN")
            or state.get("launcher_refresh")
        )
        self.member = {}
        self.user = {}
        self.game_access = None
        self.game_refresh = None
        self.game_ttl_s = None
        self.guid = state.get("guid")
        self.member_no = state.get("member_no")

    # ---- headers / parse ----
    def _headers(self, extra=None) -> dict:
        h = {
            "Accept": "*/*",
            "Accept-Encoding": "deflate, gzip",
            "Accept-Language": "zh-CN",
            "Content-Type": "application/json",
            "User-Agent": "STOVEAPIClient/3.0",
            "Caller-ID": CALLER_ID,
            "Caller-Detail": _caller_detail(self.state),
            "X-Lang": "zh-cn",
            "X-Nation": self.gds["nation"],
            "X-Timezone": self.gds["timezone"],
            "X-Utc-Offset": str(self.gds["utc_offset"]),
            "Transaction-ID": self.session_tid,
        }
        if extra:
            h.update(extra)
        return dict(sorted(h.items(), key=lambda kv: kv[0].lower()))

    def _parse(self, r, tag: str) -> dict:
        try:
            data = r.json()
        except Exception:
            # Never surface the response body: these endpoints have just handled
            # tokens and a proxy/error page could echo one back.
            try:
                ctype = r.headers.get("Content-Type", "")
            except Exception:
                ctype = ""
            raise RuntimeError(
                f"[{tag}] non-JSON response (HTTP {r.status_code}, content-type {ctype!r})"
            ) from None
        if not isinstance(data, dict):
            # A JSON array/string would otherwise raise AttributeError on .get(),
            # which main() does not catch.
            raise RuntimeError(f"[{tag}] unexpected non-object JSON response")
        return data

    # ---- QR login ----
    def qr_create(self) -> dict:
        body = {
            "login_type": "QR_LOGIN_LAUNCHER",
            "gds_info": self.gds,
            "width": 180,
            "height": 180,
        }
        r = self.s.post(
            API + "/auth-secure/v1.0/qr", json=body, headers=self._headers(), timeout=15
        )
        data = self._parse(r, "qr_create")
        _check_http(r, "qr_create")
        _check_code(data, "qr_create")
        value = data.get("value", {})
        return value if isinstance(value, dict) else {}

    def qr_status(self, session: str) -> dict:
        r = self.s.get(
            API + "/auth-secure/v1.0/qr/status",
            params={"session": session},
            headers=self._headers(),
            timeout=15,
        )
        data = self._parse(r, "qr_status")
        value = data.get("value")
        if not (isinstance(value, dict) and value.get("status")):
            # No explicit state: this is an error/lock/rate-limit envelope, not
            # a live QR session. Never let it reach the state machine.
            _check_http(r, "qr_status")
            _check_code(data, "qr_status")
        return data

    def signin_qr(self, qr_session: str) -> dict:
        body = {
            "client_id": CLIENT_ID,
            "service_id": "Launcher",
            "provider_cd": "QR",
            "provider_data": {"qr_login_session": qr_session},
            "gds_info": self.gds,
        }
        r = self.s.post(
            API_BASE + "/sign/v2.1/pc/signin", json=body, headers=self._headers(), timeout=15
        )
        data = self._parse(r, "signin")
        _check_code(data, "signin")
        self._apply_signin(data)
        return data

    def login_qr(self, timeout_s: int = 180, png_path: Path | None = None) -> None:
        import base64

        v = self.qr_create()
        sess = v.get("session")
        url = v.get("url")
        if not sess:
            raise RuntimeError(f"qr_create returned no session: {list(v)}")
        png_path = png_path or _default_qr_png()
        if v.get("image"):
            try:
                _write_private_bytes(png_path, base64.b64decode(v["image"]))
                say("\n=== STOVE QR login ===")
                say("Open this image and scan it with the STOVE mobile app:")
                say(f"    {png_path}")
            except Exception:
                png_path = None
        else:
            # No image in the response: do not announce a file that was never
            # written (the GUI would show nothing and wait for the whole timeout).
            png_path = None
        if not png_path and not url:
            raise RuntimeError("STOVE returned no QR code or login link")
        if url:
            say("Or open this link in the STOVE app:")
            say(f"    {url}")
        say(f"Waiting for the scan (up to {timeout_s}s)...\n")
        event("qr", png=str(png_path) if png_path else None, url=url, timeout=timeout_s)
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            st = self.qr_status(sess)
            value = st.get("value")
            if not isinstance(value, dict):
                value = {}
            # The free-form ``message`` is for reporting only, never a state.
            state = str(value.get("status") or "").lower()
            if state != last:
                say(f"  qr: {state}")
                event("qr_status", state=state)
                last = state
            if state in ("success", "complete", "completed", "done"):
                self.signin_qr(sess)
                self._persist_launcher()
                say("\n[+] Logged in. launcher refresh_token handed to the credential sink.")
                event("logged_in")
                try:
                    if png_path and png_path.exists():
                        png_path.unlink()
                except Exception:
                    pass
                return
            if state in ("expired", "close", "closed", "cancel", "cancelled"):
                raise ReloginRequired(f"QR login ended: {state}")
            time.sleep(2)
        raise RuntimeError("QR login timed out")

    # ---- token refresh + game-token exchange ----
    def renew(self):
        if not self.launcher_refresh:
            raise ReloginRequired("no launcher refresh_token; run `login` first")
        body = {
            "client_id": CLIENT_ID,
            "service_id": "Launcher",
            "provider_cd": "RT",
            "provider_data": {"refresh_token": self.launcher_refresh},
            "gds_info": self.gds,
        }
        r = self.s.post(
            API_BASE + "/sign/v2.1/pc/signin", json=body, headers=self._headers(), timeout=15
        )
        data = self._parse(r, "signin_RT")
        code = str(data.get("code", "0"))
        if code not in ("0", "None") and code != RELOGIN_CODE:
            raise RuntimeError(f"renew failed code={code} msg={data.get('message')}")
        _check_code(data, "renew")  # 44010 -> ReloginRequired
        self._apply_signin(data)
        self._persist_launcher()

    def game_check(self):
        for attempt in range(2):
            body = {
                "device_info": {"device_key": str(self.session_tid)},
                "gds_info": {
                    "is_default": bool(self.gds.get("is_default", False)),
                    "nation": self.gds["nation"],
                    "regulation": self.gds["regulation"],
                    "timezone": self.gds["timezone"],
                    "utc_offset": int(self.gds["utc_offset"]),
                    "lang": self.gds["lang"],
                },
                "skip_provider_check": False,
                "skip_session_check": False,
            }
            extra = {
                "Authorization": "bearer " + str(self.launcher_access),
                "market-name": "PC_MARKET",
                "Captcha-Token": "",
            }
            r = self.s.post(
                API_BASE + "/gc/v1.4/check/" + GAME_ID,
                json=body,
                headers=self._headers(extra),
                timeout=20,
            )
            data = self._parse(r, "gc_check")
            # Normalise the code: STOVE may send it as a string ("0"/"70702").
            code = str(data.get("code", "0"))
            if code == "70702" and attempt == 0:
                self.state["caller_detail"] = secrets.token_hex(20)
                _save_state(self.state)
                say("[*] gc/check 70702 -> rotated caller_detail, retrying")
                continue
            if code not in ("0", "None"):
                if code == RELOGIN_CODE:
                    _check_code(data, "gc/check")  # -> ReloginRequired (exit 3)
                raise RuntimeError(f"gc/check failed code={code} msg={data.get('message')}")
            v = data.get("value")
            v = v if isinstance(v, dict) else {}
            self.game_access = v.get("access_token")
            self.game_refresh = v.get("refresh_token")
            self.game_ttl_s = _ttl_seconds(v.get("expires_in"), v.get("expire_in"))
            member = v.get("member") if isinstance(v.get("member"), dict) else {}
            user = v.get("user") if isinstance(v.get("user"), dict) else {}
            if not user and member.get("user"):
                user = member["user"]
            self.member = member or self.member
            g = user.get("user_id") or user.get("userId") or member.get("guid")
            if g:
                self.guid = str(g)
            if member.get("member_no"):
                self.member_no = str(member["member_no"])
            if not self.game_access:
                raise RuntimeError("gc/check returned no game access_token")
            return
        raise RuntimeError("gc/check failed after caller_detail rotation")

    # ---- response plumbing ----
    def _apply_signin(self, data: dict) -> None:
        flat = {}

        def walk(o):
            if isinstance(o, dict):
                for k, val in o.items():
                    if k in (
                        "access_token",
                        "refresh_token",
                        "member_no",
                        "guid",
                        "user_id",
                        "userId",
                        "nickname",
                        "expires_in",
                        "expire_in",
                        "country_cd",
                        "account_type",
                        "reg_dt",
                        "birth_dt",
                        "person_verify_yn",
                        "email_verify_yn",
                        "parent_verify_yn",
                    ) and isinstance(val, (str, int)):
                        flat.setdefault(k, val)
                    elif isinstance(val, (dict, list)):
                        walk(val)
            elif isinstance(o, list):
                for it in o:
                    walk(it)

        walk(data)
        if flat.get("access_token"):
            self.launcher_access = flat["access_token"]
        if flat.get("refresh_token"):
            self.launcher_refresh = flat["refresh_token"]
        if not self.member_no and (flat.get("member_no")):
            self.member_no = str(flat["member_no"])
        v = data.get("value") or {}
        m = v.get("member") if isinstance(v.get("member"), dict) else {}
        if m:
            self.member = m
        self._signin_flat = flat

    def _nickname(self, member: dict) -> str:
        """The account nickname, from wherever the sign-in response put it."""
        flat = getattr(self, "_signin_flat", None) or {}
        user = self.user if isinstance(self.user, dict) else {}
        return _first_str(
            member.get("nickname"),
            member.get("member_nickname"),
            user.get("nickname"),
            flat.get("nickname"),
        )

    def _persist_launcher(self) -> None:
        # Never persist the launcher refresh token: it belongs in the OS
        # keyring (owned by the backend). The state file keeps only non-secret
        # identifiers and the install fingerprint.
        self.state.pop("launcher_refresh", None)
        if self.guid:
            self.state["guid"] = self.guid
        if self.member_no:
            self.state["member_no"] = self.member_no
        # Preserve the fresh credential first, even if the state write fails.
        self.write_credential()
        _save_state(self.state)

    def write_credential(self) -> None:
        """Hand the rotated credential to the embedding backend, if asked.

        ``CZN_CREDENTIAL_OUT`` names a file the launcher owns and deletes; it
        is written ``0600``. Keeping this off stdout/argv is deliberate.
        """
        out = os.environ.get("CZN_CREDENTIAL_OUT")
        if not out or not self.launcher_refresh:
            return
        payload = json.dumps(
            {"refresh_token": self.launcher_refresh, "member_no": str(self.member_no or "")}
        )
        _write_private(Path(out), payload)

    # ---- session.json assembly ----
    def write_session_json(self, out: Path = SESSION_JSON) -> dict:
        """Prefer splicing the fresh game token into an existing proven template
        (keeps this account's exact field set); fall back to building fresh."""
        now_ms = int(time.time() * 1000)
        expire_s = self.game_ttl_s or DEFAULT_TTL_S
        expire_ms = expire_s * 1000
        template = None
        for cand in (
            out,
            out.with_suffix(".json.expired-bak"),
            out.parent / "session.json.expired-bak",
        ):
            try:
                t = json.loads(cand.read_text())
                if isinstance(t, dict) and "access_token" in t:
                    template = t
                    break
            except Exception:
                pass
        if template:
            info = dict(template)
            info["access_token"] = self.game_access
            info["refresh_token"] = self.game_refresh or info.get("refresh_token")
            if self.guid:
                info["guid"] = self.guid
            if self.member_no:
                info["member_no"] = self.member_no
            info["caller_id"] = CALLER_ID
            info["caller_detail"] = _caller_detail(self.state)
            info["expire_in"] = str(expire_s)
            info["expires_in"] = str(expire_ms)
            info["expire_time"] = str(now_ms + expire_ms)
            info["ref_session_id"] = str(uuid.uuid4())
        else:
            m = self.member or {}
            info = {
                "caller_id": CALLER_ID,
                # The real launcher hands the game its install fingerprint too
                # (the SDK logs it as callerDetail; ours was empty).
                "caller_detail": _caller_detail(self.state),
                "game_id": GAME_ID,
                "market_game_id": MARKET_GAME_ID,
                "env": "live",
                "game_type": "ONLINE",
                "heartbeat_interval": "300",
                "playtime_interval": "1800",
                "playtime_retry_interval": "300",
                "exit_when_launcher_yn": "y",
                "ref_session_id": str(uuid.uuid4()),
                "ref_source_type": "stove_launcher",
                "uuid": "",
                "service_protocol": "game",
                "studio_game_yn": "n",
                "gds_isdefault_yn": "n",
                "gds_nation": self.gds["nation"],
                "gds_regulation": self.gds["regulation"],
                "gds_timezone": self.gds["timezone"],
                "gds_utcoffset": str(self.gds["utc_offset"]),
                "language": self.gds["lang"],
                "gds_language": self.gds["lang"],
                "access_token": self.game_access,
                "refresh_token": self.game_refresh or "",
                "expire_in": str(expire_s),
                "expires_in": str(expire_ms),
                "expire_time": str(now_ms + expire_ms),
                "account_type": "11",
                "guid": self.guid or "",
                "cloud_saving_path": "",
                "provider_cd": "SO",
                "country_cd": m.get("country_cd", self.gds["nation"]),
                "member_no": str(self.member_no or m.get("member_no") or "0"),
                "member_nickname": self._nickname(m) or "0",
                "person_verify_yn": "n",
                "parent_verify_yn": "n",
                "email_verify_yn": "y",
                "reg_dt": str(m.get("reg_dt") or ""),
                "birth_dt": str(m.get("birth_dt") or ""),
                "streaming_yn": "n",
            }
        _write_private(out, json.dumps(info, separators=(",", ":")))
        return info


#: Game-token lifetime used when gc/check does not report one.
DEFAULT_TTL_S = 21599


def _ttl_seconds(*raw) -> int | None:
    """The server's token lifetime in seconds, or None when absent/implausible.

    STOVE has reported both seconds and milliseconds; anything above a week
    is read as milliseconds."""
    for value in raw:
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if n > 7 * 86400:
            n //= 1000
        if 60 <= n <= 7 * 86400:
            return n
    return None


def _first_str(*values) -> str:
    for value in values:
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _session_info(path: Path):
    try:
        d = json.loads(path.read_text())
        exp = int(d.get("expire_time", "0")) // 1000
        return {
            "path": str(path),
            "expires_at": exp,
            "valid": bool(d.get("access_token")) and exp > time.time(),
        }
    except Exception:
        return None


def main(argv=None) -> int:
    global JSON_MODE
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "action",
        nargs="?",
        default="token",
        choices=("login", "token", "restore", "status"),
        help="login: QR login (once). token: refresh+exchange and "
        "write session.json (default). restore: refresh only, validate the "
        "saved credential. status: show state.",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=SESSION_JSON,
        help="session.json path (default: next to this script)",
    )
    ap.add_argument("--qr-png", type=Path, help="where to write the QR image")
    ap.add_argument("--qr-timeout", type=int, default=180)
    ap.add_argument(
        "--json",
        action="store_true",
        help="machine-readable JSON lines on stdout (see module docstring)",
    )
    ap.add_argument(
        "--credential-out",
        type=Path,
        help="0600 file that receives the rotated refresh token (same as "
        "CZN_CREDENTIAL_OUT); required for login/token/restore",
    )
    args = ap.parse_args(argv)
    JSON_MODE = args.json
    if args.credential_out:
        os.environ["CZN_CREDENTIAL_OUT"] = str(args.credential_out)
    if args.action != "status" and not os.environ.get("CZN_CREDENTIAL_OUT"):
        # The refresh token rotates on every use; without a sink the new one
        # would be dropped and the account logged out after this run.
        e = RuntimeError(
            "no credential sink: set CZN_CREDENTIAL_OUT or pass --credential-out FILE "
            "to receive the rotated refresh token"
        )
        say(f"[!] {e}")
        event("error", kind="no_credential_sink", message=str(e))
        return 2

    state = _load_state()
    auth = StoveAuth(state)

    if args.action == "status":
        sess = _session_info(args.out)
        if JSON_MODE:
            event(
                "status",
                logged_in=bool(auth.launcher_refresh),
                region={"nation": auth.gds["nation"], "timezone": auth.gds["timezone"]},
                session=sess,
            )
            return 0
        print("state file:", STATE_FILE, "(exists)" if STATE_FILE.exists() else "(none)")
        token_note = "present" if auth.launcher_refresh else "MISSING — run `login`"
        print("launcher refresh_token:", token_note)
        print("guid:", auth.guid or "?", "| member_no:", auth.member_no or "?")
        print("region:", auth.gds["nation"], auth.gds["timezone"])
        if sess:
            print(
                "session:",
                sess["path"],
                "valid until" if sess["valid"] else "EXPIRED at",
                time.strftime("%F %T", time.localtime(sess["expires_at"])),
            )
        return 0

    def fail(kind: str, code: int, e: Exception) -> int:
        say(f"[!] {e}")
        event("error", kind=kind, message=str(e))
        return code

    try:
        if args.action == "restore":
            auth.renew()
            auth.write_credential()
            if JSON_MODE:
                event(
                    "status",
                    logged_in=True,
                    region={"nation": auth.gds["nation"], "timezone": auth.gds["timezone"]},
                )
            return 0
        if args.action == "login":
            auth.login_qr(timeout_s=args.qr_timeout, png_path=args.qr_png)
            # go straight on to mint a game token + session.json
        auth.renew()
        auth.game_check()
        info = auth.write_session_json(args.out)
        auth.write_credential()
    except ReloginRequired as e:
        return fail("relogin_required", 3, e)
    except RequestException as e:
        return fail("network", 4, e)
    except RuntimeError as e:
        return fail("auth_failed", 2, e)
    except OSError as e:
        # A read-only/full data dir must not escape as a Python traceback.
        return fail("auth_failed", 2, e)
    exp = int(info.get("expire_time", "0")) // 1000
    say(
        f"[+] wrote {args.out} — keys: {len(info)} | "
        f"access_token present: {bool(info.get('access_token'))} | "
        f"expires: {time.strftime('%F %T', time.localtime(exp))}"
    )
    event("session", path=str(args.out), expires_at=exp, expires_in=max(0, exp - int(time.time())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
