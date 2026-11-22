#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Rebuild the index's art candidates from each publisher's *current* art.

The index (``catalog/art.json``) pins URLs, and many carry a content hash that changes when the
publisher refreshes the key visual. This script asks the publisher itself for the newest art and
writes a fresh document, so a stale cover is one command instead of a hand-edited commit:

* Steam: ``IStoreBrowseService/GetItems`` names the current ``library_capsule`` (portrait
  poster) and ``library_hero`` (wide key art), including the content-hashed paths that the
  fixed ``/steam/apps/<id>/library_<name>.jpg`` names cannot reach.
* Everything else: the existing candidate stays as it is. ``cover`` candidates are reordered
  portrait-first (the library card is 2:3) and the landscape ones are copied to ``wide`` so the
  store tile and game page hero have the right shape.

``--check`` resolves the same data and compares it with the document without writing anything,
so a scheduled job can report "a game's art changed" before anyone notices a dead URL.

Usage::

    scripts/refresh_art_index.py --seed docs/art-index.seed.json --output /tmp/art.json
    scripts/refresh_art_index.py --seed /path/nadare-index/catalog/art.json --check
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from nadare.core import (  # noqa: E402
    artindex,
    artwork,
    directkinds,  # noqa: E402
)

#: Valve's canonical sizes for the assets named by the store-browse API. The document needs a
#: plausible width/height, and these do not change between games.
STEAM_SIZES: dict[str, tuple[int, int]] = {
    "library_capsule": (300, 450),
    "library_capsule_2x": (600, 900),
    "main_capsule": (616, 353),
    "main_capsule_2x": (1232, 706),
    "header": (460, 215),
    "header_2x": (920, 430),
    "library_hero": (1920, 620),
    "library_hero_2x": (3840, 1240),
    "raw_page_background": (1920, 1080),
    "page_background": (1920, 1080),
    "logo": (640, 360),
}

#: Steam-hosted catalog games that are not in ``directkinds.ART`` (they run through another
#: publisher channel, but Steam still publishes the current portrait capsule and key art).
EXTRA_STEAM: dict[str, int] = {
    "stellasora-yostar": 4813850,
}

#: Where Steam serves the content-hashed assets the store-browse API names.
STEAM_ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"

#: Which asset names make the best portrait card and wide key art, best first.
STEAM_COVER: tuple[str, ...] = (
    "library_capsule_2x",
    "library_capsule",
    "main_capsule_2x",
    "main_capsule",
    "header_2x",
    "header",
)
STEAM_WIDE: tuple[str, ...] = (
    "library_hero_2x",
    "library_hero",
    "raw_page_background",
    "main_capsule_2x",
)


def _candidate(url: str, width: int, height: int) -> dict[str, Any]:
    return {
        "url": url,
        "orientation": "portrait" if height > width else "wide",
        "width": width,
        "height": height,
    }


def _resolved(assets: dict[str, str]) -> dict[str, str]:
    """Asset name -> absolute URL.

    :func:`artwork.steam_library_assets` already returns absolute URLs. A raw
    ``IStoreBrowseService`` answer instead carries Valve's ``asset_url_format`` plus bare
    filenames, so resolve those too: the script stays usable against a captured response.
    """
    fmt = str(assets.get("asset_url_format") or "")
    base = ""
    if fmt.startswith("steam/apps/") and "${FILENAME}" in fmt:
        base = STEAM_ASSET_BASE + fmt
    out: dict[str, str] = {}
    for name, value in assets.items():
        if name == "asset_url_format" or not isinstance(value, str) or not value:
            continue
        if value.startswith("https://"):
            out[name] = value
        elif base:
            out[name] = base.replace("${FILENAME}", value)
    return out


def _steam_field(assets: dict[str, str], names: tuple[str, ...]) -> list[dict[str, Any]]:
    """The candidates the Steam asset table provides, in preference order."""
    resolved = _resolved(assets)
    found = []
    for name in names:
        url = resolved.get(name)
        size = STEAM_SIZES.get(name)
        if not url or size is None:
            continue
        found.append(_candidate(url, *size))
    return found


def steam_kinds() -> dict[str, int]:
    """Catalog kinds whose index art may come from Steam, with their app id."""
    kinds = dict(EXTRA_STEAM)
    for kind, appid in directkinds.ART.items():
        if appid is None or kind not in artindex.ALLOWED_HOSTS:
            continue
        hosts = artindex.ALLOWED_HOSTS[kind]
        if not set(hosts) & set(artindex.STEAM_HOSTS):
            continue  # the index may not point this game at Steam (a code change would)
        kinds[kind] = int(appid)
    return kinds


def _ordered(cover: list[Any], *, portrait_first: bool) -> list[Any]:
    """The existing candidates, portrait entries first when asked, at most the app's limit."""
    candidates = [entry for entry in cover if isinstance(entry, dict)]
    if portrait_first:
        candidates.sort(key=lambda entry: entry.get("orientation") != "portrait")
    return candidates[: artindex.MAX_CANDIDATES]


def build_document(
    base: dict[str, Any],
    steam_assets: dict[str, dict[str, str]],
    *,
    date: str,
) -> dict[str, Any]:
    """Merge ``steam_assets`` into ``base`` and make every entry carry cover + wide art.

    ``steam_assets`` maps a game kind to an asset table (name -> URL), as
    :func:`artwork.steam_library_assets` returns it. Games without new data keep their
    candidates, with covers ordered portrait-first and landscape art copied to ``wide``.
    """
    games: dict[str, Any] = {}
    for kind, entry in (base.get("games") or {}).items():
        if not isinstance(entry, dict):
            continue
        fresh_cover = _steam_field(steam_assets.get(kind) or {}, STEAM_COVER)
        fresh_wide = _steam_field(steam_assets.get(kind) or {}, STEAM_WIDE)
        cover = (fresh_cover or _ordered(entry.get("cover") or [], portrait_first=True))[
            : artindex.MAX_CANDIDATES
        ]
        wide = (fresh_wide or _ordered(entry.get("wide") or [], portrait_first=False))[
            : artindex.MAX_CANDIDATES
        ]
        if not wide:
            # No separate wide art yet: reuse the landscape candidates so the store tile and
            # the game page hero have the right shape (a portrait cover is not one).
            wide = [c for c in cover if c.get("orientation") == "wide"][
                : artindex.MAX_CANDIDATES
            ]
        out: dict[str, Any] = {}
        if cover:
            out["cover"] = cover
        if wide:
            out["wide"] = wide
        if out:
            games[kind] = out
    return {"schema": artindex.SCHEMA, "updated": date, "games": games}


def fetch_steam_assets(kinds: dict[str, int]) -> dict[str, dict[str, str]]:
    """Ask Steam for each game's current assets (never raises per game)."""
    found: dict[str, dict[str, str]] = {}
    for kind, appid in sorted(kinds.items()):
        assets = artwork.steam_library_assets(appid)
        if assets:
            found[kind] = assets
        else:
            print(f"art index: no Steam assets for {kind} ({appid})", file=sys.stderr)
    return found


def load(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not artindex.valid_document(doc):
        raise ValueError(f"{path} is not a schema-1 art document")
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", required=True, type=Path, help="the current art document")
    parser.add_argument("--output", type=Path, help="write the refreshed document here")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report whether the document differs from today's publisher art",
    )
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    args = parser.parse_args(argv)
    if not args.check and args.output is None:
        parser.error("give --output (or --check)")

    try:
        base = load(args.seed)
    except (OSError, ValueError) as exc:
        print(f"art index: cannot read {args.seed}: {exc}", file=sys.stderr)
        return 2
    date = args.date or dt.date.today().isoformat()
    try:
        fresh = build_document(base, fetch_steam_assets(steam_kinds()), date=date)
    except Exception as exc:  # noqa: BLE001 - a broken network must not look like a change
        print(f"art index: could not refresh: {exc}", file=sys.stderr)
        return 2

    if not artindex.parse(fresh):
        print("art index: the refreshed document has no usable art", file=sys.stderr)
        return 2
    if args.check:
        changed = {
            kind
            for kind, entry in fresh["games"].items()
            if entry != (base.get("games") or {}).get(kind)
        }
        for kind in sorted(changed):
            print(f"art index: {kind} has new art in the index")
        print(f"art index: {len(changed)} game(s) differ" if changed else "art index: up to date")
        return 1 if changed else 0
    args.output.write_text(json.dumps(fresh, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"art index: wrote {args.output} ({len(fresh['games'])} games, {date})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
