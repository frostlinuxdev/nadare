#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Refresh a checkout of the public Nadare index repo (yostar/, czn/, webview2/, vcredist/).

Runs the Yostar manifest generator and the CZN latest-version probe, merges the
results into ``<index>/yostar`` and ``<index>/czn/latest.json``, and reports
whether anything really changed. Files whose content only differs in their
timestamp are left alone, and a game that fails this run keeps its previously
published entry, so a flaky upstream never empties the index.

The Yostar salts come from the environment (``SALT_<TAG>``); this script never
prints them. Committing and pushing is left to the caller::

    python scripts/publish_index.py --index ../nadare-index && \\
        git -C ../nadare-index commit -am "..." && git -C ../nadare-index push

Exit status: 0 = changed, 3 = nothing to publish, anything else = error.

The optional catalog section (``--catalog``) publishes the latest version of
each direct-download game and a media file pointing at its official cover art
into ``<index>/catalog``. It is off by default, runs at most once per
``--catalog-max-age-hours``, and keeps a game's previously published entry when
its resolver or image check fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

REPO = Path(__file__).resolve().parents[1]
GENERATOR = REPO / "ci" / "yostar-manifests" / "generate.py"
UNCHANGED = 3
#: The subprocess's dedicated "nothing to publish" exit: both the Yostar
#: generator (see generate.py) and the CZN latest probe (see czn update.py)
#: return it when there is genuinely nothing new; every other non-zero exit is
#: a real failure that must not be swallowed.
NOTHING_TO_GENERATE = 2
_VOLATILE = ("generated_at", "checked_at")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` in one step: a temp file in the same directory,
    fsynced, then ``os.replace``. A crash or full disk leaves the old file whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_history(dest: Path) -> dict[str, Any]:
    """The previously published ``latest.json`` (``{}`` when absent).

    A file that exists but is not a JSON object is *refused*, not treated as
    empty: resetting would discard the accepted hash history and make older
    clients fail verification.
    """
    if not dest.exists():
        return {}
    try:
        value = json.loads(dest.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError(
            f"{dest} is not valid JSON ({exc}); refusing to reset the hash history. "
            "Restore it from git, then re-run."
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{dest} is not a JSON object; refusing to reset the hash history.")
    return value


def _stable(data: bytes) -> Any:
    """The JSON content without its timestamps, for change detection."""
    payload = json.loads(data)
    if isinstance(payload, dict):
        payload = {k: v for k, v in payload.items() if k not in _VOLATILE}
    return payload


def _merge_file(new: Path, dest: Path) -> bool:
    """Copy ``new`` over ``dest`` unless only timestamps differ."""
    data = new.read_bytes()
    if dest.exists() and _stable(dest.read_bytes()) == _stable(data):
        return False
    _atomic_write_bytes(dest, data)
    return True


def publish_yostar(out: Path, env: dict[str, str]) -> bool:
    out.mkdir(parents=True, exist_ok=True)
    index_path = out / "index.json"
    index: dict[str, Any] = {}
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    changed = False
    with tempfile.TemporaryDirectory(prefix="nadare-yostar-") as tmp:
        try:
            subprocess.run(
                [sys.executable, "-I", str(GENERATOR), "--out", tmp], check=True, env=env
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode == NOTHING_TO_GENERATE:
                # Legitimate all-skipped run: keep the last-good published
                # files and report "nothing to publish" instead of crashing.
                print(
                    "yostar: generator had nothing to publish; keeping the "
                    "previously published index",
                    file=sys.stderr,
                )
                return False
            # Any other non-zero exit is a real generator failure (an
            # uncaught exception, a refused empty index, or a merge that
            # dropped a published tag).  Surface it so CI fails instead of
            # silently absorbing the error as "nothing to publish".
            raise RuntimeError(
                f"yostar: generator failed (exit {exc.returncode}); see the error above"
            ) from exc
        fresh = json.loads((Path(tmp) / "index.json").read_text(encoding="utf-8"))
        for tag, entry in fresh.items():
            changed |= _merge_file(Path(tmp) / entry["url"], out / entry["url"])
            kept = (out / entry["url"]).read_bytes()
            new_entry = {
                "version": entry["version"],
                "url": entry["url"],
                "sha256": hashlib.sha256(kept).hexdigest(),
            }
            if entry.get("media"):
                changed |= _merge_file(Path(tmp) / entry["media"], out / entry["media"])
                new_entry["media"] = entry["media"]
            elif index.get(tag, {}).get("media"):
                new_entry["media"] = index[tag]["media"]  # keep last good media
            index[tag] = new_entry
        missing = sorted(set(index) - set(fresh))
        for tag in missing:
            print(f"yostar {tag}: not refreshed this run, keeping the published entry")
    text = json.dumps(index, indent=2, sort_keys=True) + "\n"
    if not index_path.exists() or index_path.read_text(encoding="utf-8") != text:
        _atomic_write_bytes(index_path, text.encode("utf-8"))
        changed = True
    return changed


def publish_czn(out: Path, env: dict[str, str]) -> bool:
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "latest.json"
    start = "55"
    if dest.exists():
        start = str(json.loads(dest.read_text(encoding="utf-8")).get("latest") or start)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "nadare.backends.czn.update", "latest", "--from", start],
            check=True,
            capture_output=True,
            text=True,
            env={**env, "PYTHONPATH": str(REPO / "src"), "CZN_MANIFEST_INDEX": ""},
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode == NOTHING_TO_GENERATE:
            # The probe's dedicated "no published manifest" exit (see
            # update.py): keep the last-good published file and report
            # "nothing to publish" instead of crashing.
            print(
                "czn: probe found nothing to publish; keeping the previously "
                "published latest.json",
                file=sys.stderr,
            )
            return False
        # Any other non-zero exit is a real probe failure; surface it so CI
        # fails instead of silently absorbing it as "nothing to publish".
        detail = (exc.stderr or "").strip()
        raise RuntimeError(
            f"czn: probe failed (exit {exc.returncode})"
            + (f": {detail}" if detail else "")
        ) from exc
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write(result.stdout.strip() + "\n")
    try:
        return _merge_file(Path(fh.name), dest)
    finally:
        os.unlink(fh.name)


WEBVIEW2_URL = "https://go.microsoft.com/fwlink/?linkid=2124701"
WEBVIEW2_HOSTS = (".microsoft.com",)
WEBVIEW2_KEEP = 4  # current + three previous hashes stay accepted


def _url_identity(url: str) -> tuple[str, str, str]:
    """Scheme, host and path of ``url``: a CDN may vary the query between requests."""
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.path


def publish_webview2(out: Path, env: dict[str, str], opener: Any = None) -> bool:
    """Record the SHA-256 of Microsoft's current WebView2 installer.

    ``out/latest.json`` lists the current hash first, then the previous ones, so
    clients on a slightly older index still verify. The (large) file is only
    downloaded when Microsoft's size/Last-Modified changed; any network or host
    problem keeps the previously published file.
    """
    opener = opener or urllib.request.urlopen
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "latest.json"
    prev: dict[str, Any] = _load_history(dest)
    try:
        with opener(urllib.request.Request(WEBVIEW2_URL, method="HEAD"), timeout=60) as r:
            host = (urllib.parse.urlparse(r.geturl()).hostname or "").lower()
            if not host.endswith(WEBVIEW2_HOSTS):
                raise RuntimeError(f"redirected off Microsoft: {r.geturl()}")
            size = int(r.headers.get("Content-Length") or 0)
            modified = r.headers.get("Last-Modified") or ""
            final_url = r.geturl()
        if urllib.parse.urlparse(final_url).scheme != "https":
            raise RuntimeError(f"non-https installer URL: {final_url}")
        hashes = [h for h in prev.get("sha256", []) if isinstance(h, str)]
        if hashes and prev.get("size") == size and prev.get("last_modified") == modified:
            if prev.get("url") == final_url:
                return False
            prev["url"] = final_url
            _atomic_write_bytes(dest, (json.dumps(prev, indent=2) + "\n").encode("utf-8"))
            return True
        digest = hashlib.sha256()
        got = 0
        with opener(WEBVIEW2_URL, timeout=120) as r:
            host = (urllib.parse.urlparse(r.geturl()).hostname or "").lower()
            if not host.endswith(WEBVIEW2_HOSTS):
                raise RuntimeError(f"redirected off Microsoft: {r.geturl()}")
            get_final_url = r.geturl()
            while chunk := r.read(1 << 20):
                digest.update(chunk)
                got += len(chunk)
        get_final = urllib.parse.urlparse(get_final_url)
        if get_final.scheme != "https":
            raise RuntimeError(f"non-https download URL: {get_final_url}")
        if _url_identity(get_final_url) != _url_identity(final_url):
            raise RuntimeError(
                "download URL changed between HEAD and GET: "
                f"{final_url} -> {get_final_url}"
            )
        if got < 50_000_000 or (size and got != size):
            raise RuntimeError(f"unexpected download size {got} (expected {size})")
    except Exception as exc:  # keep the last-good file on any failure
        print(f"webview2: {exc}; keeping the previously published latest.json", file=sys.stderr)
        return False
    new = digest.hexdigest()
    kept = [h for h in hashes if h != new]
    payload = {
        "sha256": [new, *kept][:WEBVIEW2_KEEP],
        "url": final_url,
        "size": got,
        "last_modified": modified,
        "checked_at": int(time.time()),
    }
    text = json.dumps(payload, indent=2) + "\n"
    if dest.exists() and _stable(dest.read_bytes()) == _stable(text.encode()):
        return False
    _atomic_write_bytes(dest, text.encode("utf-8"))
    return True


VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
VCREDIST_HOSTS = (".microsoft.com",)
VCREDIST_KEEP = 8  # current + seven previous hashes stay accepted
VCREDIST_MIN_SIZE = 10_000_000  # the real installer is ~25 MB


def _vcredist_host_ok(url: str) -> bool:
    """True when ``url`` is on Microsoft (aka.ms or a *.microsoft.com host)."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host == "aka.ms" or host.endswith(VCREDIST_HOSTS)


def publish_vcredist(out: Path, env: dict[str, str], opener: Any = None) -> bool:
    """Record the SHA-256 of Microsoft's current VC++ x64 redistributable.

    ``out/latest.json`` lists the current hash first, then the previous ones, so
    clients on a slightly older index still verify. The (large) file is only
    downloaded when Microsoft's size/Last-Modified changed; any network or host
    problem keeps the previously published file.
    """
    opener = opener or urllib.request.urlopen
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "latest.json"
    prev: dict[str, Any] = _load_history(dest)
    try:
        with opener(urllib.request.Request(VCREDIST_URL, method="HEAD"), timeout=60) as r:
            if not _vcredist_host_ok(r.geturl()):
                raise RuntimeError(f"redirected off Microsoft: {r.geturl()}")
            size = int(r.headers.get("Content-Length") or 0)
            modified = r.headers.get("Last-Modified") or ""
            final_url = r.geturl()
        if urllib.parse.urlparse(final_url).scheme != "https":
            raise RuntimeError(f"non-https installer URL: {final_url}")
        hashes = [h for h in prev.get("sha256", []) if isinstance(h, str)]
        if hashes and prev.get("size") == size and prev.get("last_modified") == modified:
            if prev.get("url") == final_url:
                return False
            prev["url"] = final_url
            _atomic_write_bytes(dest, (json.dumps(prev, indent=2) + "\n").encode("utf-8"))
            return True
        digest = hashlib.sha256()
        got = 0
        with opener(VCREDIST_URL, timeout=120) as r:
            if not _vcredist_host_ok(r.geturl()):
                raise RuntimeError(f"redirected off Microsoft: {r.geturl()}")
            get_final_url = r.geturl()
            while chunk := r.read(1 << 20):
                digest.update(chunk)
                got += len(chunk)
        get_final = urllib.parse.urlparse(get_final_url)
        if get_final.scheme != "https":
            raise RuntimeError(f"non-https download URL: {get_final_url}")
        if _url_identity(get_final_url) != _url_identity(final_url):
            raise RuntimeError(
                "download URL changed between HEAD and GET: "
                f"{final_url} -> {get_final_url}"
            )
        if got < VCREDIST_MIN_SIZE or (size and got != size):
            raise RuntimeError(f"unexpected download size {got} (expected {size})")
    except Exception as exc:  # keep the last-good file on any failure
        print(f"vcredist: {exc}; keeping the previously published latest.json", file=sys.stderr)
        return False
    new = digest.hexdigest()
    kept = [h for h in hashes if h != new]
    payload = {
        "sha256": [new, *kept][:VCREDIST_KEEP],
        "url": final_url,
        "size": got,
        "last_modified": modified,
        "checked_at": int(time.time()),
    }
    text = json.dumps(payload, indent=2) + "\n"
    if dest.exists() and _stable(dest.read_bytes()) == _stable(text.encode()):
        return False
    _atomic_write_bytes(dest, text.encode("utf-8"))
    return True


class CatalogGame(NamedTuple):
    """One catalog game: its title, resolver and Steam id."""

    title: str
    resolve: Callable[[], object]
    steam_appid: int | None


_HOYO_BIZ = {
    "genshin": "hk4e_global",
    "hsr": "hkrpg_global",
    "hi3": "bh3_global",
    "zzz": "nap_global",
}


def default_catalog_games() -> dict[str, CatalogGame]:
    """The catalog table, built from the backend registry (no network access)."""
    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from nadare.core import directkinds as dk
    from nadare.core.sources import (
        aethergazer,
        bd2,
        endfield,
        gfl2,
        hoyo,
        mongil,
        nte,
        pgr,
        wuwa,
    )

    modules = {
        "bd2": bd2,
        "aethergazer": aethergazer,
        "mongil": mongil,
        "wuwa": wuwa,
        "pgr": pgr,
        "gfl2": gfl2,
        "gfl2-haoplay": types.SimpleNamespace(resolve=gfl2.resolve_haoplay),
        "endfield": endfield,
        "nte": nte,
    }
    games: dict[str, CatalogGame] = {}
    for kind, steam_appid in dk.ART.items():
        if not kind.startswith("direct-"):
            continue
        if kind in dk.HOYO_KINDS and not dk.hoyo_visible():
            continue  # NADARE_HIDE_HOYO=1 hides them, like the Add Game wizard
        tag = kind.removeprefix("direct-")
        if kind not in dk._DIRECT or (tag not in modules and tag not in _HOYO_BIZ):
            print(f"catalog {tag}: no resolver for this kind yet, skipping")
            continue
        if tag in modules:
            module = modules[tag]

            def resolve(module=module) -> object:
                return module.resolve().version

        else:
            biz = _HOYO_BIZ[tag]

            def resolve(biz=biz) -> object:
                return hoyo.resolve(biz).version

        games[tag] = CatalogGame(
            title=dk._DIRECT[kind][1],
            resolve=resolve,
            steam_appid=steam_appid,
        )
    return games


def _differs_stable(path: Path, data: bytes) -> bool:
    """True when ``path`` is missing, not valid JSON, or differs from ``data`` beyond timestamps."""
    if not path.exists():
        return True
    try:
        return _stable(path.read_bytes()) != _stable(data)
    except ValueError:
        return True


def _load_catalog_failures(path: Path) -> dict[str, dict[str, Any]]:
    """The per-tag failure counts (``{}`` when the file is missing or corrupt)."""
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        tag: entry
        for tag, entry in value.items()
        if isinstance(tag, str) and isinstance(entry, dict) and isinstance(entry.get("count"), int)
    }


def _write_catalog_failures(path: Path, failures: dict[str, dict[str, Any]]) -> None:
    """Atomically persist the failure counts."""
    text = json.dumps(failures, indent=2, sort_keys=True) + "\n"
    _atomic_write_bytes(path, text.encode("utf-8"))


def _report_catalog_failures(
    failures: dict[str, dict[str, Any]], fail_threshold: int
) -> None:
    """Print a canary line for every source that has failed enough runs in a row."""
    for tag in sorted(failures):
        entry = failures[tag]
        count = entry["count"]
        if count >= fail_threshold:
            reason = str(entry.get("reason", ""))
            print(f"CANARY: catalog source {tag} has failed {count} runs in a row ({reason})")


def publish_catalog(
    out: Path,
    env: dict[str, str],
    *,
    games: dict[str, CatalogGame] | None = None,
    now: float | None = None,
    deadline: float = 300.0,
    clock: Callable[[], float] = time.monotonic,
    failures_state: Path | None = None,
    fail_threshold: int = 3,
) -> bool:
    """Publish ``<out>/index.json`` and per-game media files for the catalog.

    A game whose resolver fails (or returns no usable version) keeps its
    previously published entry and media; a media file whose content only
    differs in ``generated_at`` is not rewritten. An empty index is never
    published. Games not reached within ``deadline`` seconds keep their published entries.
    When ``failures_state`` is set, consecutive resolver failures are counted there and a
    ``CANARY:`` line is printed once a source reaches ``fail_threshold``.
    Returns True only when a file was written.
    """
    games = default_catalog_games() if games is None else games
    now = time.time() if now is None else now
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    index_path = out / "index.json"
    failures = _load_catalog_failures(failures_state) if failures_state is not None else {}
    previous_failures = {tag: dict(entry) for tag, entry in failures.items()}
    try:
        index = _load_history(index_path)
    except RuntimeError as exc:
        print(f"catalog: {exc}; rebuilding the index from this run")
        index = {}
    changed = False
    successful: dict[str, Any] = {}
    started = clock()
    for tag in sorted(games):
        if clock() - started > deadline:
            print(f"catalog {tag}: deadline reached, keeping the published entries")
            break
        game = games[tag]
        try:
            version = game.resolve()
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            print(
                f"catalog {tag}: not refreshed this run ({reason}), keeping the published entry"
            )
            if failures_state is not None:
                failures[tag] = {
                    "count": failures.get(tag, {}).get("count", 0) + 1,
                    "reason": reason[:120],
                }
            continue
        if not isinstance(version, str) or not version.strip():
            print(
                f"catalog {tag}: not refreshed this run (no usable version), "
                "keeping the published entry"
            )
            if failures_state is not None:
                failures[tag] = {
                    "count": failures.get(tag, {}).get("count", 0) + 1,
                    "reason": "no usable version",
                }
            continue
        version = version.strip()
        if failures_state is not None:
            failures.pop(tag, None)
        media_path = out / f"{tag}.media.json"
        media: dict[str, Any] = {
            "game_tag": tag,
            "title": game.title,
            "generated_at": generated_at,
        }
        if game.steam_appid is not None:
            media["steam_appid"] = game.steam_appid
        data = (json.dumps(media, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if _differs_stable(media_path, data):
            _atomic_write_bytes(media_path, data)
            changed = True
        successful[tag] = {
            "title": game.title,
            "version": version,
            "media": f"{tag}.media.json",
        }
    index.update(successful)
    if failures_state is not None:
        if failures != previous_failures:
            _write_catalog_failures(failures_state, failures)
        _report_catalog_failures(failures, fail_threshold)
    if not index:
        return False
    text = json.dumps(index, indent=2, sort_keys=True) + "\n"
    if not index_path.exists() or index_path.read_text(encoding="utf-8") != text:
        _atomic_write_bytes(index_path, text.encode("utf-8"))
        changed = True
    return changed


def catalog_due(state: Path, max_age_hours: float, now: float | None = None) -> bool:
    """True when the catalog has not been checked within ``max_age_hours``."""
    now = time.time() if now is None else now
    if max_age_hours <= 0 or not state.exists():
        return True
    return now - state.stat().st_mtime >= max_age_hours * 3600


def mark_catalog_checked(state: Path, now: float | None = None) -> None:
    """Record a successful catalog check in ``state``."""
    now = time.time() if now is None else now
    state.parent.mkdir(parents=True, exist_ok=True)
    state.touch(exist_ok=True)
    os.utime(state, (now, now))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", required=True, type=Path, help="nadare-index checkout")
    parser.add_argument("--skip-czn", action="store_true")
    parser.add_argument("--skip-yostar", action="store_true")
    parser.add_argument("--skip-webview2", action="store_true")
    parser.add_argument("--skip-vcredist", action="store_true")
    parser.add_argument("--catalog", action="store_true")
    parser.add_argument("--catalog-force", action="store_true")
    parser.add_argument("--catalog-max-age-hours", type=float, default=6)
    parser.add_argument("--catalog-fail-threshold", type=int, default=3)
    args = parser.parse_args(argv)

    env = dict(os.environ)
    changed = False
    if not args.skip_yostar:
        changed |= publish_yostar(args.index / "yostar", env)
    if not args.skip_czn:
        changed |= publish_czn(args.index / "czn", env)
    if not args.skip_webview2:
        changed |= publish_webview2(args.index / "webview2", env)
    if not args.skip_vcredist:
        changed |= publish_vcredist(args.index / "vcredist", env)
    if args.catalog:
        state_root = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        state = state_root / "nadare-index" / "catalog-checked"
        if not args.catalog_force and not catalog_due(state, args.catalog_max_age_hours):
            print("catalog: checked recently, skipping")
            # A broken source must keep turning the hourly heartbeat down, not only every 6 h.
            _report_catalog_failures(
                _load_catalog_failures(state_root / "nadare-index" / "catalog-failures.json"),
                args.catalog_fail_threshold,
            )
        else:
            try:
                changed |= publish_catalog(
                    args.index / "catalog",
                    env,
                    failures_state=state_root / "nadare-index" / "catalog-failures.json",
                    fail_threshold=args.catalog_fail_threshold,
                )
            except Exception as exc:  # noqa: BLE001 - the catalog is optional; keep the other steps
                print(
                    f"catalog: failed ({type(exc).__name__}: {exc}); other steps unaffected",
                    file=sys.stderr,
                )
            else:
                mark_catalog_checked(state)
    print("changed" if changed else "nothing to publish")
    return 0 if changed else UNCHANGED


if __name__ == "__main__":
    raise SystemExit(main())
