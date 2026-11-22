#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 czn-linux contributors
"""czn_update.py — STOVE-free game updater for Chaos Zero Nightmare.

Reimplements STOVE's "dpms" download/patch protocol (tokenless, md5-verified)
so the game files can be installed/updated without the STOVE launcher. This is
the last STOVE dependency; see docs/czn-pcsdk-ipc.md ("Plan: STOVE-free install/
updates").

  check   (default) — report installed vs latest version and which files differ
  update --apply    — download+verify+write the changed files
  latest [--from N] — print {"service","latest","checked_at"} as JSON: what the
                      czn-manifests job publishes (probes STOVE from N upward)

Exit codes: 0 up to date / updated, 1 a download failed, 2 setup or network
error (CDN unreachable, no game dir), 3 the game is running (nothing written),
5 the game folder is missing, 6 another install or update holds this game's
folder (nothing written).

This covers the client binaries (~20 files, ~270 MB). Game content is patched
by the game itself at startup and needs nothing from here.

Mechanism (all from unauthenticated endpoints, headers caller-id + caller-detail):
  version:  the latest manifest is STOVE_CHAOSZERO_<n>.json on the game CDN;
            probe upward from the installed version to find it. With
            CZN_MANIFEST_INDEX set, read the number from that latest.json first
            (published by a scheduled job, so users don't each probe STOVE);
            it is checked against the CDN and probing is the fallback.
  manifest: <CDN>/game/dpms_STOVE_CHAOSZERO/STOVE_CHAOSZERO_<n>.json
            -> build metadata + a `files` list of pipe rows:
               F: idx | F | /path | ver_folder | chunk | gz | usize | psize |
                  md5_unpacked | md5_packed | ...
               D: idx | D | /path
  chunks:   <CDN>/game/dpms_STOVE_CHAOSZERO/v<ver_folder>/<chunk>.gz
            -> the gzip of one client file.

The download engine is the shared one (#1557): the manifest becomes a
:class:`nadare.core.directdl.Plan` whose files are ``Packed("gzip")`` (md5 of the
packed and of the unpacked bytes), :func:`directdl.sync` fetches it under the
shared install lock, the on-disk check is directdl's hashing, and "the game is
running" is the backend's own rule (``backend_parts.launching``). What stays here
is CZN's own: finding the version, reading and refusing manifests, the stat cache
that lets the launch-time check skip unchanged files, the game's version marker
(the upf) and the manifest copy Verify uses.

Files are synced by comparing each manifest file's md5_unpacked to the on-disk
file, so it works for a fresh install or an update. vcredist/directx/dotnet
prereqs are skipped (Wine provides them).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import NamedTuple

try:
    from nadare.core import diskspace, tls
except Exception:  # pragma: no cover - standalone script use
    diskspace = None  # type: ignore[assignment]
    tls = None  # type: ignore[assignment]

from nadare.core import directdl, hosting
from nadare.core.events import Event
from nadare.core.yostar import ManifestError, safe_relative_path

from .backend_parts import launching

try:
    from curl_cffi import requests as creq
    from curl_cffi.const import CurlHttpVersion

    _SESS = creq.Session(
        impersonate="chrome", default_headers=False, http_version=CurlHttpVersion.V1_1
    )
except Exception:
    _SESS = None

import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

SERVICE = os.environ.get("CZN_SERVICE", "STOVE_CHAOSZERO")
CDN = os.environ.get("CZN_DL_CDN", "https://chaoszero-dl.game.onstove.com")
BASE = f"{CDN}/game/dpms_{SERVICE}"


def _xdg_fallback(env: str, default: str) -> Path:
    """XDG base used only when ``nadare.core.paths`` cannot be imported.

    Mirrors core.paths so a direct-script run honours ``XDG_DATA_HOME`` rather
    than hard-coding ``~/.local/share``.
    """
    return Path(os.environ.get(env) or os.path.expanduser(default)) / "nadare"


# Identify like the official launcher does on these requests: its version string
# and this install's caller-detail (the one czn_auth.py keeps), not a fresh id per run.
try:
    from .auth import CALLER_ID, STATE_FILE  # package / `-m nadare.backends.czn.update`
except ImportError:  # pragma: no cover - direct-script use
    CALLER_ID, STATE_FILE = (
        "STOVE_LAUNCHER_VER.3.2.30.737",
        Path(
            os.environ.get("CZN_STATE_DIR", "")
            or _xdg_fallback("XDG_DATA_HOME", "~/.local/share") / "czn"
        )
        / "czn_auth.json",
    )
try:
    CALLER_DETAIL = json.loads(STATE_FILE.read_text()).get("caller_detail") or ""
except Exception:
    CALLER_DETAIL = ""
CALLER_ID = os.environ.get("CZN_UPDATER_CALLER", CALLER_ID)
CALLER_DETAIL = (
    CALLER_DETAIL if re.fullmatch(r"[0-9a-f]{40}", CALLER_DETAIL) else secrets.token_hex(20)
)
# The published default lives in nadare.core.hosting; CZN_MANIFEST_INDEX overrides it
# (set it empty to always probe STOVE instead of using the published pointer).


def index_url() -> str:
    """CZN_MANIFEST_INDEX if set (empty disables), else Nadare's published pointer."""
    value = os.environ.get("CZN_MANIFEST_INDEX")
    if value is None:
        return hosting.czn_index_url()
    return value.strip()


def _default_prefix() -> Path:
    try:
        from nadare.core import paths

        return paths.data_dir() / "czn" / "prefix"
    except Exception:
        return _xdg_fallback("XDG_DATA_HOME", "~/.local/share") / "czn" / "prefix"


PREFIX = Path(os.environ.get("CZN_PREFIX", str(_default_prefix())))
GAME_DIR = PREFIX / "drive_c/ProgramData/Smilegate/Games/ChaosZeroNightmare"
UPF = GAME_DIR / "combinedata_manifest" / f"GameManifest_{SERVICE}.upf"
#: Copy of the last applied manifest, so ``verify`` can hash the install offline.
MANIFEST_CACHE = GAME_DIR / "combinedata_manifest" / f"nadare-manifest-{SERVICE}.json"
#: Stat fingerprints of files whose content hash matched the current manifest.
#: Stored outside GAME_DIR so game updates do not overwrite verification state.
STAT_CACHE = PREFIX / f"nadare-verified-files-{SERVICE}.json"

HEADERS = {
    "caller-id": CALLER_ID,
    "caller-detail": CALLER_DETAIL,
    "User-Agent": "STOVEAPIClient/3.0",
    "Accept": "*/*",
}
PREREQ_PREFIXES = ("vcredist", "directx", "dotnet", "ueprereq", "addons")
#: Safety cap on how many manifest probes one ``latest`` lookup may make, so a
#: fresh install with no usable index cannot hammer the CDN.
MAX_PROBES = 200

#: Exit codes the backend words for the user (``backend.UPDATE_EXIT_MESSAGES``).
EXIT_RUNNING = 3
EXIT_BUSY = 6
#: A manifest md5 column must be exactly this, or the row is rejected (fail closed).
_MD5_RE = re.compile(r"\A[0-9a-fA-F]{32}\Z")
#: A version folder or chunk name goes into the chunk URL, so it must be a plain token.
_TOKEN_RE = re.compile(r"\A[0-9A-Za-z_.-]{1,64}\Z")
#: How the b27-and-older updater staged a download: ``<file>.part.gz`` held the packed bytes
#: (resumable), ``<file>.part`` the unpacked ones. Adopted or removed once (#1557).
_LEGACY_PACKED = ".part.gz"
_LEGACY_UNPACKED = ".part"


#: Default read cap for manifests/JSON; file downloads pass a larger one.
MAX_TEXT_BYTES = 16 << 20


def _get(url: str, timeout: int = 60, max_bytes: int = MAX_TEXT_BYTES) -> bytes:
    if _SESS is not None:
        r = _SESS.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            raise FileNotFoundError(f"{r.status_code} {url}")
        if len(r.content) > max_bytes:
            raise ValueError(f"response too large for {url}")
        return r.content
    req = urllib.request.Request(url, headers=HEADERS)
    with (
        tls.urlopen(req, timeout=timeout, context=tls.ssl_context())
        if tls is not None
        else urllib.request.urlopen(req, timeout=timeout)
    ) as resp:  # noqa: S310
        body = resp.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"response too large for {url}")
    return body


_PROBE_LOCAL = threading.local()


def _probe_session():
    """A curl session private to this thread (one session is not shared across threads)."""
    session = getattr(_PROBE_LOCAL, "session", None)
    if session is None:
        session = creq.Session(
            impersonate="chrome", default_headers=False, http_version=CurlHttpVersion.V1_1
        )
        _PROBE_LOCAL.session = session
    return session


def _head_ok(url: str) -> bool:
    try:
        if _SESS is not None:
            r = _probe_session().get(url, headers=HEADERS, timeout=12)
            return r.status_code == 200
        _get(url, timeout=12)
        return True
    except Exception:
        return False


def local_version() -> int:
    try:
        return int(json.loads(UPF.read_text()).get("local_version", 0))
    except Exception:
        return 0


def latest_version(installed: int, *, max_probes: int = MAX_PROBES) -> int:
    """Highest published STOVE_CHAOSZERO_<n>.json, probing upward from installed.

    Probing stops after five consecutive misses or ``max_probes`` requests,
    whichever comes first, so a fresh install with no usable index does not
    fan out one request per integer.
    """
    probes = 0
    latest = 0
    if probes < max_probes:
        probes += 1
        if _head_ok(f"{BASE}/{SERVICE}_{installed}.json"):
            latest = installed
    v = installed
    # Look at five versions at a time: a missing key costs a full round trip, so walking
    # them one by one made the check several seconds long. The result is the same as the
    # one-at-a-time walk (stop after five consecutive misses).
    while probes < max_probes:
        window = list(range(v + 1, v + 1 + min(5, max_probes - probes)))
        probes += len(window)
        with ThreadPoolExecutor(max_workers=len(window)) as pool:
            hits = list(pool.map(lambda n: _head_ok(f"{BASE}/{SERVICE}_{n}.json"), window))
        found = [n for n, hit in zip(window, hits, strict=False) if hit]
        if not found:
            break
        latest = v = max(found)
    return latest


def indexed_version() -> int | None:
    """The latest version number from index_url(), or None if unset/unusable."""
    url = index_url()
    if not url.startswith("https://"):
        return None
    try:  # no STOVE headers: this isn't STOVE
        if _SESS is not None:
            r = _SESS.get(url, timeout=10)
            body = r.content if r.status_code == 200 else b""
        else:
            with (
                tls.urlopen(url, timeout=10, context=tls.ssl_context())
                if tls is not None
                else urllib.request.urlopen(url, timeout=10)
            ) as resp:  # noqa: S310
                body = resp.read(MAX_TEXT_BYTES + 1)[:MAX_TEXT_BYTES]
        d = json.loads(body)
        n = d.get("latest")
        if d.get("service") == SERVICE and isinstance(n, int) and n > 0:
            return n
    except Exception:
        pass
    return None


def resolve_latest(installed: int) -> tuple[int, str]:
    """(latest version, where the number came from). The unsigned index is only
    a hint: it is trusted when it is *strictly newer* than what is installed
    (so a tampered index cannot pin ``latest`` at the installed version and
    suppress updates) and STOVE really publishes that manifest."""
    n = indexed_version()
    if n and n > installed and _head_ok(f"{BASE}/{SERVICE}_{n}.json"):
        return n, "index"
    return latest_version(installed), "probe"


def fetch_manifest(version: int) -> dict:
    return json.loads(_get(f"{BASE}/{SERVICE}_{version}.json").decode("utf-8"))


def check_manifest(manifest: object) -> None:
    """Refuse anything that is not a manifest: an error page, a maintenance notice, junk.

    Such a document used to plan as "nothing differs", so the updater called it up to date,
    advanced the installed version and cached it as the inventory to verify against (#904).
    """
    if not isinstance(manifest, dict):
        raise ManifestError("the server answered with something that is not a manifest")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(r, str) for r in files):
        raise ManifestError("the manifest has no file list (the server may be in maintenance)")
    version = manifest.get("version_no")
    if version is not None:
        try:
            int(version)
        except (TypeError, ValueError):
            raise ManifestError(f"the manifest's version is not a number: {version!r}") from None


def parse_files(manifest: dict):
    """Yield (kind, path, ver_folder, chunk, md5_unpacked, md5_packed, usize)."""
    for row in manifest.get("files", []):
        p = [x.strip() for x in row.split("|")]
        if len(p) < 2:
            continue
        kind, path = p[1], p[2] if len(p) > 2 else ""
        if kind == "D":
            yield ("D", path, None, None, None, None, None)
        elif kind == "F" and len(p) >= 10:
            try:
                usize = int(p[6] or 0)
            except ValueError as e:
                # A malformed size must be reported, not crash with a traceback.
                raise ManifestError(f"invalid file size in manifest: {p[6]!r}") from e
            yield ("F", path, p[3], p[4], p[8], p[9], usize)


def _rel(path: str) -> Path:
    """Resolve a manifest path under GAME_DIR, refusing traversal.

    CZN manifest paths are root-relative ("/bin/foo"), so a single leading
    slash is dropped before the shared validator, which rejects absolute
    paths, drive letters, ``..`` and anything that escapes the install root.
    """
    text = path.strip()
    if text.startswith("/"):
        text = text[1:]
    rel = Path(safe_relative_path(text))
    root = GAME_DIR.resolve()
    target = (GAME_DIR / rel).resolve()
    if target != root and root not in target.parents:
        raise ManifestError(f"manifest path escapes the game dir: {path!r}")
    return GAME_DIR / rel


def _load_stat_cache() -> dict[str, dict[str, int | str]]:
    """Load verified-file fingerprints; malformed or missing state is a miss."""
    try:
        raw = json.loads(STAT_CACHE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        return {}
    files = raw.get("files")
    if not isinstance(files, dict):
        return {}
    return {
        path: row
        for path, row in files.items()
        if isinstance(path, str)
        and isinstance(row, dict)
        and isinstance(row.get("md5"), str)
    }


def _write_stat_cache(files: dict[str, dict[str, int | str]]) -> None:
    """Atomically save successful file checks; failure only costs performance."""
    tmp: str | None = None
    try:
        STAT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=STAT_CACHE.parent, delete=False
        ) as stream:
            tmp = stream.name
            json.dump({"schema": 1, "files": files}, stream, separators=(",", ":"))
        os.replace(tmp, STAT_CACHE)
    except OSError:
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


class Row(NamedTuple):
    """One manifest file still to fetch, as :func:`plan` returns it."""

    path: str
    vf: str | None
    ci: str | None
    md5u: str | None
    md5p: str | None
    usize: int | None


def _relative(path: str) -> str:
    """A manifest path as directdl takes it: relative to GAME_DIR, no leading slash."""
    return _rel(path).relative_to(GAME_DIR).as_posix()


def plan(manifest: dict, trust_stat_cache: bool = True):
    """Return (to_download, dirs) comparing manifest md5s to on-disk files.

    The check of a file's bytes is directdl's (:func:`directdl.plan_missing`, #1557).
    ``trust_stat_cache`` lets the launch-time check skip hashing a file whose stat
    fingerprint matches its last verified state. Verify and Repair pass ``False``:
    they must read every file, since silent corruption leaves the stat unchanged.
    The cache is still refreshed from what was actually hashed.
    """
    dirs: list[str] = []
    stored = _load_stat_cache()
    cached = stored if trust_stat_cache else {}
    verified: dict[str, dict[str, int | str]] = {}
    #: Manifest order; each entry: (row, fingerprint, what to check or None = fetch it).
    entries: list[tuple[Row, dict[str, int | str], directdl.RemoteFile | None]] = []
    for kind, path, vf, ci, md5u, md5p, usize in parse_files(manifest):
        try:
            dst = _rel(path)  # reject absolute/traversal paths from the manifest
        except (ValueError, OSError) as e:
            raise ManifestError(f"invalid manifest path: {path!r}") from e
        if kind == "D":
            dirs.append(path)
            continue
        if path.lstrip("/").lower().startswith(PREREQ_PREFIXES):
            continue
        expected = (md5u or "").lower()
        try:
            stat = dst.stat()
            fingerprint: dict[str, int | str] = {
                "md5": expected,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
                "ino": stat.st_ino,
                "dev": stat.st_dev,
            }
        except OSError:
            fingerprint = {}
        if fingerprint and cached.get(path) == fingerprint:
            verified[path] = fingerprint
            continue
        row = Row(path, vf, ci, md5u, md5p, usize)
        if not _MD5_RE.fullmatch(expected):
            # No usable hash: never "matches"; fetching it then refuses the row (fail closed).
            entries.append((row, fingerprint, None))
            continue
        try:
            relative = _relative(path)
            check = directdl.RemoteFile(relative, "", md5=expected)
            directdl.safe_relative_path(relative)
        except (ValueError, directdl.DirectError) as e:
            raise ManifestError(f"invalid manifest path: {path!r}") from e
        entries.append((row, fingerprint, check))
    checks = tuple(check for _row, _fp, check in entries if check is not None)
    try:
        missing, _archives = directdl.plan_missing(
            directdl.Plan(SERVICE, "check", files=checks), GAME_DIR, verify=True
        )
    except directdl.DirectError as e:  # a duplicate path
        raise ManifestError(str(e)) from e
    differ = {rf.path for rf in missing}
    to_download: list[Row] = []
    for row, fingerprint, check in entries:
        if check is None or check.path in differ:
            to_download.append(row)
        elif fingerprint:
            verified[row.path] = fingerprint
    if verified != stored:
        _write_stat_cache(verified)
    return to_download, dirs


def _manifest_md5(value: str, path: str, kind: str) -> str:
    """A manifest md5 column, lowercased; an empty/malformed one is rejected.

    Failing closed matters: the streaming fetch must reject an empty column as
    "nothing to check" and wrote the chunk unverified.
    """
    if not _MD5_RE.fullmatch(value or ""):
        raise ManifestError(f"manifest has no valid {kind} md5 for {path}")
    return value.lower()


def to_plan(rows, version: object = "") -> directdl.Plan:
    """The files of ``rows`` (from :func:`plan`) as a directdl plan of gzip chunks.

    Every row must carry both md5s and a plain version folder and chunk name, or the
    manifest is refused (:class:`ManifestError`): nothing is fetched unchecked.
    """
    files = []
    headers = tuple(HEADERS.items())
    for row in rows:
        path, vf, ci, md5u, md5p, usize = tuple(row)[:6]
        md5p = _manifest_md5(md5p, path, "packed")
        md5u = _manifest_md5(md5u, path, "unpacked")
        if not (_TOKEN_RE.fullmatch(str(vf or "")) and _TOKEN_RE.fullmatch(str(ci or ""))):
            raise ManifestError(f"manifest has no valid chunk name for {path}")
        try:
            relative = _relative(path)
        except (ValueError, OSError) as e:
            raise ManifestError(f"invalid manifest path: {path!r}") from e
        files.append(
            directdl.RemoteFile(
                relative,
                f"{BASE}/v{vf}/{ci}.gz",
                int(usize) if usize is not None else None,
                md5=md5u,
                headers=headers,
                packed=directdl.Packed("gzip", md5=md5p),
            )
        )
    plan_ = directdl.Plan(SERVICE, str(version), files=tuple(files))
    try:
        directdl._validate_plan(plan_)
    except directdl.DirectError as e:
        raise ManifestError(str(e)) from e
    return plan_


def _settle_legacy_staging(manifest: dict, fetching: set[str]) -> None:
    """Deal with what an interrupted b27-or-older download left (#1557 upgrade).

    ``<file>.part.gz`` held the first bytes of the packed chunk: for a file this run fetches it
    becomes directdl's resumable ``<file>.nadare-gz.part`` (the same bytes), otherwise it goes.
    ``<file>.part`` (unpacked, never resumable) goes. Only names of manifest files are touched,
    and a link is removed, never followed.
    """
    for kind, path, *_rest in parse_files(manifest):
        if kind != "F":
            continue
        try:
            dst = _rel(path)
        except (ValueError, OSError):
            continue
        old_unpacked = dst.with_name(dst.name + _LEGACY_UNPACKED)
        old_packed = dst.with_name(dst.name + _LEGACY_PACKED)
        if old_unpacked.is_symlink() or old_unpacked.is_file():
            old_unpacked.unlink(missing_ok=True)
        if not (old_packed.is_symlink() or old_packed.is_file()):
            continue
        resume = dst.with_name(dst.name + directdl.PACKED_SUFFIX + ".part")
        if path in fetching and not old_packed.is_symlink() and not resume.exists():
            try:
                os.replace(old_packed, resume)
                continue
            except OSError:
                pass
        old_packed.unlink(missing_ok=True)


def _progress_printer():
    """directdl events as this module's output lines (the backend reads ``[done/total]``)."""
    last = {"step": -1}

    def emit(event: Event) -> None:
        data = event.data if isinstance(event.data, dict) else {}
        if event.kind == "progress":
            done, total = data.get("done"), data.get("total")
            if not (isinstance(done, int) and isinstance(total, int) and total > 0):
                return
            step = done * 50 // total
            if step == last["step"]:
                return
            last["step"] = step
            print(f"  [{done // 1000}/{total // 1000}] kB downloaded", flush=True)
        elif event.kind == "step":
            print(f"[*] {event.message}", flush=True)
        elif event.kind == "error":
            print(f"[!] {event.message}", flush=True)
        else:
            print(f"  {event.message}", flush=True)

    return emit


def game_running() -> bool:
    """The game of *this* install runs: the backend's own rule (``launching.game_running``),
    so the updater and the game page cannot disagree (#1557)."""
    return launching.game_running(PREFIX, GAME_DIR)


def _cache_manifest(manifest: dict) -> None:
    try:
        MANIFEST_CACHE.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_CACHE.write_text(json.dumps(manifest))
    except OSError:
        pass  # verify just reports "not checked"


def _verify_cached() -> int:
    """Hash the install against the cached manifest; print one JSON event line."""
    try:
        manifest = json.loads(MANIFEST_CACHE.read_text())
        check_manifest(manifest)
        differ, _dirs = plan(manifest, trust_stat_cache=False)
    except (OSError, ValueError, ManifestError):
        print(json.dumps({"event": "verify", "checked": False}))
        return 0
    print(
        json.dumps(
            {
                "event": "verify",
                "checked": True,
                "version": manifest.get("version_no"),
                "count": len(differ),
                "differ": [d[0] for d in differ[:50]],
            }
        )
    )
    return 0


def _write_local_version(manifest: dict, target: int) -> None:
    """Bump the local manifest version marker (the upf) to this manifest."""
    try:
        upf = json.loads(UPF.read_text()) if UPF.exists() else {}
        upf["local_version"] = int(manifest.get("version_no", target))
        upf.setdefault("game_id", SERVICE)
        UPF.parent.mkdir(parents=True, exist_ok=True)
        # Write a sibling file and swap it in: a full disk or a kill mid-write
        # must not leave the game's manifest truncated.
        fd, tmp = tempfile.mkstemp(dir=str(UPF.parent), prefix=f".{UPF.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(upf, indent=4))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, UPF)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception as e:
        print(f"[!] could not update {UPF.name}: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "action", nargs="?", default="check", choices=("check", "update", "latest", "verify")
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="with `update`: actually download and write (default is dry-run)",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="hash every file instead of trusting the verified-files cache (used by Repair)",
    )
    ap.add_argument("--version", type=int, help="target a specific manifest version")
    ap.add_argument(
        "--from",
        dest="start",
        type=int,
        default=1,
        help="with `latest`: probe upward from this version (the last known one)",
    )
    args = ap.parse_args(argv)

    if args.action == "latest":
        n = latest_version(args.start)
        if not n:
            print(f"[!] no published {SERVICE} manifest found from v{args.start}", file=sys.stderr)
            return 2
        print(json.dumps({"service": SERVICE, "latest": n, "checked_at": int(time.time())}))
        return 0

    if args.action == "verify":
        return _verify_cached()

    if not GAME_DIR.is_dir():
        if args.action == "update" and args.apply and (PREFIX / "drive_c").is_dir():
            GAME_DIR.mkdir(parents=True)  # fresh install into an existing prefix
        else:
            print(
                f"[!] game dir not found: {GAME_DIR}\n"
                "    the game files are missing or incomplete; press Install in Nadare "
                "to repair them"
            )
            return 5

    inst = local_version()
    try:
        target, source = (args.version, "requested") if args.version else resolve_latest(inst)
        if not target:
            raise OSError("no manifest found")
    except Exception as e:
        print(f"[!] cannot reach the update server ({e})")
        return 2
    print(f"service: {SERVICE} | installed: v{inst} | latest: v{target} ({source})")
    if target < inst and not args.version:
        print("[!] could not find a published manifest >= installed; CDN may have changed")
        return 2

    try:
        manifest = fetch_manifest(target)
    except Exception as e:
        print(f"[!] manifest fetch failed: {e}")
        return 2
    print(
        f"manifest v{manifest.get('version_no')} | root: {manifest.get('root_folder')} "
        f"| exec: {manifest.get('execution')}"
    )

    try:
        check_manifest(manifest)
        to_dl, dirs = plan(manifest, trust_stat_cache=not args.full)
    except ManifestError as e:
        print(f"[!] manifest rejected: {e}")
        return 2

    total = sum(int(row[5] or 0) for row in to_dl)
    if not to_dl:
        count = sum(1 for _ in parse_files(manifest))
        print(f"[+] up to date — all {count} entries match on disk.")
        if args.action == "update" and args.apply:
            # The files already match this manifest; still advance the marker,
            # or the status keeps showing the old number when a release
            # changed nothing under the client files.
            try:
                with directdl.install_lock(GAME_DIR):
                    _write_local_version(manifest, target)
            except directdl.InstallBusy as e:
                print(f"[!] {e}")
                return EXIT_BUSY
            except directdl.DirectError as e:  # an unusable lock folder
                print(f"[!] {e}")
                return 1
        _cache_manifest(manifest)
        return 0
    print(f"[*] {len(to_dl)} file(s) differ / missing, {total / 1e6:.1f} MB to fetch:")
    for row in to_dl[:40]:
        print(f"    {row[0]}  (v{row[1]}/{row[2]}.gz)")
    if len(to_dl) > 40:
        print(f"    … and {len(to_dl) - 40} more")

    if args.action != "update" or not args.apply:
        print("\n(dry-run — re-run `update --apply` to download and write)")
        return 0
    try:
        sync_plan = to_plan(to_dl, manifest.get("version_no", target))
    except ManifestError as e:
        print(f"[!] manifest rejected: {e}")
        return 2
    try:
        with directdl.install_lock(GAME_DIR):
            return _apply(manifest, target, sync_plan, to_dl, dirs, total)
    except directdl.InstallBusy as e:  # another sync holds this folder; nothing written
        print(f"[!] {e}")
        return EXIT_BUSY
    except directdl.DirectError as e:  # an unusable lock folder, or a failed download
        print(f"[!] {e}")
        return 1


def _apply(manifest: dict, target: int, sync_plan, to_dl, dirs, total: int) -> int:
    """Write the update; the caller holds the install lock for GAME_DIR."""
    if game_running():
        print("[!] the game is running; close it first (its files are in use)")
        return EXIT_RUNNING

    if diskspace is not None:
        try:
            diskspace.check_space(GAME_DIR, total)
        except Exception as exc:  # noqa: BLE001 - report, never traceback
            print(f"[!] {exc}")
            return 1

    for p in dirs:
        try:
            _rel(p).mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001 - report, never traceback
            print(f"  [!] {p}: {e}")
            return 1
    _settle_legacy_staging(manifest, {row[0] for row in to_dl})
    try:
        summary = directdl.sync(
            sync_plan,
            GAME_DIR,
            emit=_progress_printer(),
            workers=directdl.default_workers(len(sync_plan.files)),
            lock=False,
        )
    except directdl.DirectError as e:
        print(f"[!] {e}")
        return 1
    _write_local_version(manifest, target)
    _cache_manifest(manifest)
    print(
        f"[+] updated to v{manifest.get('version_no')} — "
        f"{summary.get('downloaded', 0)} file(s) fetched."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
