#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 czn-linux contributors
"""czn_runners.py — download a Proton build for Chaos Zero Nightmare from GitHub.

  list                      the runners on offer, installed ones, and newer upstream releases
  install <id>              the tested release of dwproton | cachyos | ge
  install <id> --latest     the newest upstream release instead (untested with the game)

Runners are installed into ~/.local/share/czn-linux/runners/<name> (override:
CZN_RUNNERS_DIR); pass that directory as CZN_PROTON to czn_direct_launch.sh.

Downloads come from each project's GitHub releases only. A tested release must
match the sha256 pinned below, so a replaced upstream file is refused; --latest
can only be checked against the .sha512sum published next to it, which catches
a broken download but not a tampered release.

--json prints one JSON event per line (progress, installed, error) for the GUI.
Exit codes: 0 ok, 1 usage or install error, 4 network error, 9 checksum mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from nadare.core import diskspace, download
from nadare.core.backends import BackendError
from nadare.core.runners import RunnerError, _github_asset_host_allowed, _published_sha512


def _default_runners_dir() -> Path:
    try:
        from nadare.core import paths

        return paths.runners_dir()
    except Exception:
        return Path.home() / ".local/share/nadare/runners"


def _default_cache_dir() -> Path:
    try:
        from nadare.core import paths

        return paths.cache_dir() / "czn"
    except Exception:
        return Path.home() / ".cache/nadare/czn"


RUNNERS_DIR = Path(os.environ.get("CZN_RUNNERS_DIR", str(_default_runners_dir())))
CACHE_DIR = Path(os.environ.get("CZN_CACHE_DIR", str(_default_cache_dir())))
API = "https://api.github.com/repos/%s/releases"

# tested: the release verified in-game, and the sha256 of its x86_64 tarball.
RUNNERS = {
    "cachyos": {
        "name": "Proton-CachyOS",
        "repo": "CachyOS/proton-cachyos",
        "asset": r"proton-cachyos-.+-x86_64\.tar\.xz",
        "tested": (
            "cachyos-11.0-20260703-slr",
            "62ff4b2750180723cc00538608fe687e21d1d91a31ef64ce1a7c9f46c3db310b",
        ),
    },
    "dwproton": {
        "name": "DW-Proton",
        "repo": "dawn-winery/dwproton-mirror",
        "asset": r"dwproton-.+-x86_64\.tar\.xz",
        "tested": (
            "dwproton-11.0-14",
            "c563cc99d464fb19a767a0eb90dc723746bfcdd402a4fdaa0737b12af081f99a",
        ),
    },
    "ge": {
        "name": "GE-Proton",
        "repo": "GloriousEggroll/proton-ge-custom",
        "asset": r"GE-Proton.+-x86_64\.tar\.gz",
        "tested": (
            "GE-Proton11-7",
            "c5448b76a230384e2d7bc6beb5ccb97bafb7e2c3b6c527cb03a1a546bbcb00a0",
        ),
    },
}

JSON = False


class Fail(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def emit(event: str, **kw) -> None:
    if JSON:
        print(json.dumps({"event": event, **kw}), flush=True)
    elif event == "progress":
        print(
            f"\r[*] {kw['stage']}: {kw['done'] >> 20} / {kw['total'] >> 20} MB",
            end="",
            flush=True,
        )
    elif event == "info":
        print("[*] " + kw["message"], flush=True)


def _get(url: str, timeout: int = 30) -> urllib.request.addinfourl:
    req = urllib.request.Request(
        url, headers={"User-Agent": "czn-linux", "Accept": "application/vnd.github+json"}
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        code = 4 if e.code >= 500 or e.code == 403 else 1
        raise Fail(code, f"{url}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise Fail(4, f"{url}: {e}") from e


def release(rid: str, tag: str | None) -> dict:
    """The GitHub release `tag` (None: latest) of a runner, with its tarball asset."""
    r = RUNNERS[rid]
    url = API % r["repo"] + ("/tags/" + tag if tag else "/latest")
    with _get(url) as resp:
        body = resp.read((16 << 20) + 1)
    if len(body) > (16 << 20):
        raise Fail(4, f"{url}: response too large")
    rel = json.loads(body)
    assets = {a["name"]: a for a in rel.get("assets", [])}
    tar = next((a for n, a in assets.items() if re.fullmatch(r["asset"], n)), None)
    if tar is None:
        raise Fail(1, f"{r['name']} {rel.get('tag_name')} has no x86_64 tarball")
    # The asset name is network-controlled and becomes a path under CACHE_DIR;
    # require a single, safe filename component (F1).
    name = str(tar.get("name") or "")
    if Path(name).name != name or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise Fail(1, f"unsafe runner asset name: {name!r}")
    stem = re.sub(r"\.tar\.(xz|gz)$", "", name)
    return {"tag": rel["tag_name"], "tar": tar, "sum": assets.get(stem + ".sha512sum")}


def installed() -> list[Path]:
    try:
        return sorted(
            d
            for d in RUNNERS_DIR.iterdir()
            if not d.name.startswith(".") and (d / "proton").is_file()
        )
    except OSError:
        return []


def _download(asset: dict, dest: Path) -> tuple[str, str]:
    """Stream an asset to `dest`; returns its (sha256, sha512)."""
    h256, h512 = hashlib.sha256(), hashlib.sha512()
    total, done, last = asset["size"], 0, 0.0
    url = str(asset.get("browser_download_url") or "")
    # The asset URL comes from release JSON, so pin it to a GitHub-controlled
    # host and keep every redirect hop there too (F2).
    if not _github_asset_host_allowed(url):
        raise Fail(1, f"refusing runner asset from untrusted host: {url}")
    try:
        response = download.http_open(
            url,
            headers={"Accept": "application/octet-stream"},
            timeout=60,
            redirect_allowed=_github_asset_host_allowed,
        )
    except download.DownloadError as e:
        raise Fail(4, str(e)) from e
    with response, open(dest, "wb") as f:
        while chunk := response.read(1 << 20):
            f.write(chunk)
            h256.update(chunk)
            h512.update(chunk)
            done += len(chunk)
            if done > (total or (8 << 30)):
                raise Fail(4, f"download exceeded its declared size ({total} bytes)")
            if time.monotonic() - last > 0.5 or done == total:
                emit("progress", stage="download", done=done, total=total)
                last = time.monotonic()
    if not JSON:
        print()
    if done != total:
        raise Fail(4, f"download incomplete: {done} of {total} bytes")
    return h256.hexdigest(), h512.hexdigest()


def _extract(tar_path: Path) -> Path:
    """Unpack into a scratch dir next to the runners, then move the Proton dir into place."""
    RUNNERS_DIR.mkdir(parents=True, exist_ok=True)
    # A unique scratch directory per call so two concurrent installs cannot
    # unpack over each other (F10).
    tmp = Path(tempfile.mkdtemp(prefix=".unpack-", dir=str(RUNNERS_DIR)))
    try:
        emit("info", message="unpacking")
        with tarfile.open(tar_path) as t:
            # "data" refuses absolute paths, links leaving tmp, device files, setuid bits
            t.extractall(tmp, filter="data")
        tops = [d for d in tmp.iterdir() if d.is_dir()]
        if len(tops) != 1 or not (tops[0] / "proton").is_file():
            raise Fail(1, "unexpected tarball layout (no <dir>/proton)")
        dest = RUNNERS_DIR / tops[0].name
        # Never delete the working install before the replacement is in place
        # (F4): move the old tree aside, swap the new one in, then remove the
        # old copy; restore the old tree if the swap fails.
        old: Path | None = None
        if dest.exists():
            old = RUNNERS_DIR / f".{dest.name}.old-{uuid.uuid4().hex}"
            os.replace(dest, old)
        try:
            os.replace(tops[0], dest)
        except BaseException:
            if old is not None and not dest.exists():
                os.replace(old, dest)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        return dest
    except tarfile.TarError as e:
        raise Fail(1, f"unpack failed: {e}") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def install(rid: str, latest: bool) -> Path:
    r = RUNNERS[rid]
    if not latest and r["tested"] is None:
        raise Fail(1, f"{r['name']} has no tested release yet; use --latest")
    tag, pinned = (None, None) if latest else r["tested"]
    rel = release(rid, tag)
    emit(
        "info",
        message=(
            f"{r['name']} {rel['tag']} ({rel['tar']['size'] >> 20} MB) from github.com/{r['repo']}"
        ),
    )
    expect512 = None
    if pinned is None:
        if rel["sum"] is None:
            raise Fail(9, f"no .sha512sum published for {rel['tag']}; refusing")
        # The sum URL comes from the same untrusted JSON: pin its host and every
        # redirect hop to GitHub (F2).
        try:
            expect512 = _published_sha512(rel["sum"])
        except RunnerError as e:
            raise Fail(1, str(e)) from e

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    size = int(rel["tar"].get("size") or 0)
    if size:
        try:
            diskspace.check_space(CACHE_DIR, size)
        except BackendError as e:
            raise Fail(4, str(e)) from e
    # A unique scratch name so concurrent installs cannot share (and unlink)
    # each other's partial download (F10).
    part = CACHE_DIR / f"{rel['tar']['name']}.{uuid.uuid4().hex}.part"
    try:
        s256, s512 = _download(rel["tar"], part)
        gh = (rel["tar"].get("digest") or "").removeprefix("sha256:")
        if (pinned and s256 != pinned) or (expect512 and s512 != expect512) or (gh and s256 != gh):
            raise Fail(9, f"checksum mismatch for {rel['tar']['name']}; nothing installed")
        source = "pinned sha256" if pinned else "published sha512"
        emit("info", message=f"checksum ok ({source})")
        dest = _extract(part)
    finally:
        part.unlink(missing_ok=True)
    emit("installed", path=str(dest), tag=rel["tag"], tested=not latest)
    if not JSON:
        print(f"[+] installed: {dest}\n    use it with: CZN_PROTON={dest}")
    return dest


def cmd_list() -> None:
    have = installed()
    rows = []
    for rid, r in RUNNERS.items():
        try:
            latest = release(rid, None)["tag"]
        except Fail:
            latest = None
        rows.append(
            {
                "id": rid,
                "name": r["name"],
                "tested": r["tested"] and r["tested"][0],
                "latest": latest,
            }
        )
    if JSON:
        print(json.dumps({"event": "list", "runners": rows, "installed": [str(d) for d in have]}))
        return
    for x in rows:
        print(
            f"{x['id']:<9} {x['name']:<15} tested: {x['tested'] or '-':<28} "
            f"latest: {x['latest'] or '(offline)'}"
        )
    print(f"installed in {RUNNERS_DIR}:")
    for d in have or []:
        print("  " + d.name)
    if not have:
        print("  (none)")


def main(argv=None) -> int:
    global JSON
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=("list", "install"))
    ap.add_argument("runner", nargs="?", choices=tuple(RUNNERS))
    ap.add_argument("--latest", action="store_true", help="newest upstream release (untested)")
    ap.add_argument("--json", action="store_true", help="JSON event lines for the GUI")
    a = ap.parse_args(argv)
    JSON = a.json
    try:
        if a.action == "list":
            cmd_list()
        elif a.runner is None:
            ap.error("install needs a runner: " + ", ".join(RUNNERS))
        else:
            install(a.runner, a.latest)
        return 0
    except Fail as e:
        if JSON:
            emit("error", message=str(e), code=e.code)
        else:
            print("\n[x] " + str(e), file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
