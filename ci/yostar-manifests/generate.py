#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""CI generator for Nadare's Yostar direct-download manifests.

The Yostar launcher API is signed with a per-game *salt*.  That salt must not
ship in a GPL client, so this scheduled job keeps it in CI secrets, calls the
three launcher endpoints, and publishes a plain, normalised manifest per game::

    index.json                    -> {tag: {"version", "url", "sha256"}}
    <GameTag>.json                -> {"game_tag", "version", "base_url",
                                      "generated_at", "files": [
                                          {"path", "size", "crc64"}]}

Three-step launcher flow (all under ``API_BASE``):

1. ``GET /api/launcher/game/config``            -> ``data.game_latest_version``,
                                                    ``data.game_latest_file_path``
2. ``GET /api/launcher/game/config/json?version=..&file_path=..`` -> ``data.url``
3. ``GET <data.url>`` (unsigned, public CDN)    -> raw ``{"source", "file": [...]}``

Errors come back as HTTP 200 with ``{"code": ..., "msg": ...}``; any response
without ``data`` is treated as a failure and the code/message is surfaced.

Salts are read from ``SALT_<TAG>`` (the tag upper-cased, non-alphanumerics
replaced by ``_``); launcher versions from ``VERSION_<TAG>``.  Games without a
salt are skipped with a warning.  The salt and the signed ``Authorization``
header are never printed or written.

``--merge-into <checkout>`` merges the freshly generated files into an existing
published checkout, keeping the last-good entry and file for any tag that was
not regenerated this run (the same policy as ``scripts/publish_index.py``); a
parsable-but-empty payload is treated as a failure, and nothing is ever deleted
wholesale.  Exit status: ``2`` when no game could be generated at all (nothing
to publish), ``1`` on a real error — including a merge that would drop an
already-published tag — and ``0`` otherwise, so a persistent upstream failure
cannot silently empty the published manifests; a newly added or bootstrap tag
only warns so a half-rolled-out game cannot wedge the job.

Run::

    python ci/yostar-manifests/generate.py --out dist/manifests
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Allow running straight from a checkout without an install step.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from nadare.core import yostar  # noqa: E402

API_BASE = "https://api-launcher-en.yo-star.com"
DEFAULT_TIMEOUT = 30

#: Exit code for "no game could be generated this run" (nothing to publish).
#: Distinct from 1 (a real error) so ``scripts/publish_index.py`` can tell a
#: legitimate all-skipped run from a broken generator; see the docstring.
NOTHING_TO_GENERATE = 2

#: Games to publish.  Imported from the client so there is one source of truth.
GAME_TAGS: dict[str, str] = dict(yostar.GAME_TAGS)


class YostarApiError(RuntimeError):
    """The launcher API returned an error envelope instead of ``data``."""

    def __init__(self, code: Any, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"Yostar API error {code}: {message}")


# ---------------------------------------------------------------------------
# low-level HTTP (patchable in tests)
# ---------------------------------------------------------------------------
USER_AGENT = "Nadare-manifests/0.1 (+yostar-manifests)"


def _http_get(
    url: str, headers: dict[str, str] | None = None, *, timeout: int = DEFAULT_TIMEOUT
) -> bytes:
    # Yostar's CDN answers 403 to Python's default "Python-urllib" agent.
    merged = {"User-Agent": USER_AGENT}
    merged.update(headers or {})
    request = urllib.request.Request(url, headers=merged)
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _unwrap(payload: Any) -> Any:
    if not isinstance(payload, dict) or "data" not in payload:
        code = payload.get("code") if isinstance(payload, dict) else None
        message = payload.get("msg") if isinstance(payload, dict) else "malformed response"
        raise YostarApiError(code, str(message))
    return payload["data"]


def _api_get(
    path: str,
    tag: str,
    version: str,
    salt: str,
    fetch,
    *,
    query: str = "",
) -> Any:
    headers = yostar.signed_headers(tag, version, salt)
    payload = json.loads(fetch(API_BASE + path + query, headers).decode("utf-8"))
    return _unwrap(payload)


def _parse_authorization(header: str) -> dict[str, Any]:
    """Inspect a signed header (tests only; never logs the salt)."""
    return json.loads(header)


# ---------------------------------------------------------------------------
# one game
# ---------------------------------------------------------------------------
def build_manifest(
    tag: str, version: str, raw: dict[str, Any], url: str, *, generated_at: str | None = None
) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise YostarApiError(None, f"manifest URL is not http(s): {url!r}")
    if not isinstance(raw, dict):
        raise YostarApiError(None, f"malformed manifest payload: {type(raw).__name__}")
    source = str(raw.get("source", "")).strip("/")
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    if source:
        base_url = base_url.rstrip("/") + "/" + source
    entries = raw.get("file")
    if entries is None:  # ``"file": null`` must skip the tag, not crash the run
        entries = []
    if not isinstance(entries, list):
        raise YostarApiError(None, "manifest file list is not a list")
    files: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            raise YostarApiError(None, f"malformed file entry: {item!r}")
        path = str(item.get("path", "")).lstrip("/")
        if not path:
            raise YostarApiError(None, "file entry has no path")
        try:
            size = int(item.get("size"))
            crc = yostar.normalize_crc64(item.get("hash", 0))
        except (TypeError, ValueError) as exc:
            raise YostarApiError(None, f"malformed file entry {path!r}: {exc}") from exc
        files.append({"path": path, "size": size, "crc64": crc})
    # A parsable-but-empty payload (``file`` missing or empty) must be treated
    # as a generation failure, not published: overwriting a good manifest with
    # an empty file list would silently break every client.
    if not files:
        raise YostarApiError(None, "manifest lists no files (empty or malformed payload)")
    stamp = generated_at or (_dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat())
    return {
        "game_tag": tag,
        "version": str(version),
        "base_url": base_url,
        "generated_at": stamp,
        "files": files,
    }


def generate_game(
    tag: str, version: str, salt: str, *, fetch=None, generated_at: str | None = None
) -> dict[str, Any]:
    """Run the three signed/unsigned steps for one game tag."""
    fetch = fetch or _http_get
    config = _api_get("/api/launcher/game/config", tag, version, salt, fetch)
    game_version = str(config["game_latest_version"])
    file_path = str(config["game_latest_file_path"])
    query = (
        "?version="
        + urllib.parse.quote(game_version)
        + "&file_path="
        + urllib.parse.quote(file_path)
    )
    info = _api_get("/api/launcher/game/config/json", tag, version, salt, fetch, query=query)
    manifest_url = str(info["url"])
    raw = json.loads(fetch(manifest_url, {}).decode("utf-8"))
    return build_manifest(tag, game_version, raw, manifest_url, generated_at=generated_at)


def generate_media(
    tag: str, version: str, salt: str, *, fetch=None, generated_at: str | None = None
) -> dict[str, Any]:
    """Fetch the launcher background and operation banners for one game tag.

    Both calls are signed exactly like the game config calls.  Media is
    best-effort: callers treat a failure as "no media" rather than a failed
    manifest.
    """
    fetch = fetch or _http_get
    base = _api_get("/api/launcher/base/config", tag, version, salt, fetch)
    operations = _api_get("/api/launcher/operations/resource", tag, version, salt, fetch)
    return build_media(tag, base, operations, generated_at=generated_at)


def build_media(
    tag: str,
    base: Any,
    operations: Any,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Normalise the base-config/operations payloads into a media document."""
    source = base if isinstance(base, dict) else {}
    ops = operations if isinstance(operations, dict) else {}
    raw_background = source.get("launcher_background_img")
    background = str(raw_background).strip() if raw_background else None
    banners: list[dict[str, str]] = []
    for item in ops.get("operations_banner_list") or []:
        if not isinstance(item, dict):
            continue
        image = item.get("banner_img")
        if not image:
            continue
        banners.append({"image": str(image), "url": str(item.get("jump_url") or "")})
    stamp = generated_at or (_dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat())
    return {
        "game_tag": tag,
        "generated_at": stamp,
        "background": background,
        "banners": banners,
    }


def write_outputs(
    manifests: list[dict[str, Any]],
    out_dir: str | os.PathLike[str],
    *,
    media: dict[str, dict[str, Any]] | None = None,
) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {}
    for manifest in manifests:
        name = f"{manifest['game_tag']}.json"
        data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write_bytes(out / name, data)
        index[manifest["game_tag"]] = {
            "version": manifest["version"],
            "url": name,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    for tag, payload in (media or {}).items():
        entry = index.get(tag)
        if entry is None:
            continue
        name = f"{tag}.media.json"
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write_bytes(out / name, data)
        entry["media"] = name
    _atomic_write_bytes(
        out / "index.json",
        (json.dumps(index, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return index


#: Keys that change every run and must not by themselves trigger a rewrite.
_VOLATILE = ("generated_at",)


def _stable(data: bytes) -> Any:
    """The JSON content without its timestamps, for change detection."""
    payload = json.loads(data)
    if isinstance(payload, dict):
        payload = {k: v for k, v in payload.items() if k not in _VOLATILE}
    return payload


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


def _merge_file(new: Path, dest: Path) -> bool:
    """Copy ``new`` over ``dest`` unless only the timestamp differs."""
    data = new.read_bytes()
    if dest.exists() and _stable(dest.read_bytes()) == _stable(data):
        return False
    _atomic_write_bytes(dest, data)
    return True


def merge_outputs(
    fresh_dir: str | os.PathLike[str],
    published_dir: str | os.PathLike[str],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Merge freshly generated manifests into a published checkout.

    Mirrors ``scripts/publish_index.py``: a tag that was not regenerated this
    run keeps its previously published entry and file, so a flaky upstream can
    never empty the index.  Only files actually refreshed are copied over.
    Returns ``(index, refreshed, kept)``.
    """
    fresh = Path(fresh_dir)
    published = Path(published_dir)
    published.mkdir(parents=True, exist_ok=True)
    fresh_index: dict[str, Any] = json.loads(
        (fresh / "index.json").read_text(encoding="utf-8")
    )
    index_path = published / "index.json"
    index: dict[str, Any] = {}
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    refreshed: list[str] = []
    for tag, entry in fresh_index.items():
        _merge_file(fresh / entry["url"], published / entry["url"])
        kept = (published / entry["url"]).read_bytes()
        new_entry: dict[str, Any] = {
            "version": entry["version"],
            "url": entry["url"],
            "sha256": hashlib.sha256(kept).hexdigest(),
        }
        if entry.get("media"):
            _merge_file(fresh / entry["media"], published / entry["media"])
            new_entry["media"] = entry["media"]
        elif index.get(tag, {}).get("media"):
            new_entry["media"] = index[tag]["media"]  # keep last good media
        index[tag] = new_entry
        refreshed.append(tag)
    kept_tags = sorted(set(index) - set(refreshed))
    for tag in kept_tags:
        print(f"{tag}: not refreshed this run, keeping the published entry", file=sys.stderr)
    _atomic_write_bytes(
        index_path,
        (json.dumps(index, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return index, refreshed, kept_tags


def _env_key(prefix: str, tag: str) -> str:
    return prefix + re.sub(r"[^A-Z0-9]", "_", tag.upper())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish Yostar direct-download manifests")
    parser.add_argument("--out", default="manifests", help="output directory")
    parser.add_argument(
        "--merge-into",
        default=None,
        help="existing published checkout to merge into (keeps last-good entries)",
    )
    parser.add_argument("--now", default=None, help="override generated_at (ISO-8601, tests)")
    args = parser.parse_args(argv)

    manifests: list[dict[str, Any]] = []
    media: dict[str, dict[str, Any]] = {}
    configured: list[str] = []
    for tag in GAME_TAGS:
        salt = os.environ.get(_env_key("SALT_", tag), "").strip()
        version = os.environ.get(_env_key("VERSION_", tag), "").strip()
        if not salt:
            print(f"skip {tag}: no SALT_{_env_key('', tag)} in the environment", file=sys.stderr)
            continue
        if not version:
            print(
                f"skip {tag}: no VERSION_{_env_key('', tag)} in the environment",
                file=sys.stderr,
            )
            continue
        # Only a tag that can actually be generated (salt *and* version) counts
        # as configured; a half-rolled-out game must not fail the publish job.
        configured.append(tag)
        try:
            manifest = generate_game(tag, version, salt, generated_at=args.now)
        except (YostarApiError, KeyError, ValueError, OSError) as exc:
            print(f"skip {tag}: {exc}", file=sys.stderr)
            continue
        manifests.append(manifest)
        try:
            media[tag] = generate_media(tag, version, salt, generated_at=args.now)
        except Exception as exc:  # noqa: BLE001 - media must never fail the manifest
            print(f"media {tag}: {exc}", file=sys.stderr)
            media.pop(tag, None)
        print(f"{tag}: {manifest['version']} ({len(manifest['files'])} files)")

    if not manifests:
        print(
            "no manifests were generated "
            f"(configured tags: {', '.join(configured) or 'none'})",
            file=sys.stderr,
        )
        return NOTHING_TO_GENERATE

    index = write_outputs(manifests, args.out, media=media)
    if not index:
        print("error: refusing to publish an empty index", file=sys.stderr)
        return 1

    if args.merge_into is not None:
        published = Path(args.merge_into)
        previously: set[str] = set()
        index_path = published / "index.json"
        if index_path.exists():
            try:
                previously = set(json.loads(index_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                previously = set()
        merged, _refreshed, _kept = merge_outputs(args.out, published)
        if not merged:
            print("error: refusing to publish an empty index", file=sys.stderr)
            return 1
        # Invariant: a merge must never drop an already-published tag.  Compare
        # every configured tag that was in the pre-merge index against the
        # merged one; if `merge_outputs` ever stops preserving a kept tag this
        # fails closed instead of silently publishing a truncated index.
        dropped = sorted(tag for tag in (set(configured) & previously) if tag not in merged)
        if dropped:
            print(
                "error: merge dropped previously published tag(s): " + ", ".join(dropped),
                file=sys.stderr,
            )
            return 1
        # A configured tag absent from the merged index has never been published
        # successfully.  Tolerate tags that were never published before (a
        # first/bootstrap run, or a newly added game) so one broken tag cannot
        # wedge the whole publish job forever.
        missing = [tag for tag in configured if tag not in merged]
        if missing:
            print(
                f"warning: configured tag(s) not yet published: {', '.join(missing)}",
                file=sys.stderr,
            )
        index = merged

    print(f"wrote {len(index)} manifest(s) to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
