# Cover art from the index

Besides the sources described in [ARTWORK.md](ARTWORK.md), Nadare reads a curated list of cover
URLs from the index: `catalog/art.json` in the `nadare-index` repository, fetched like the other
index files (two mirrors, last good copy kept on disk).

Why: many of the best official URLs carry a content hash in their name and stop working when the
publisher updates the art. A dead URL is fixed with an index commit, not a release.

## Format

```json
{
  "schema": 1,
  "updated": "2026-10-28",
  "games": {
    "direct-genshin": {
      "cover": [
        {"url": "https://cdn2.unrealengine.com/...jpg", "orientation": "wide", "width": 2560, "height": 1440},
        {"url": "https://cdn2.unrealengine.com/...jpg", "orientation": "portrait", "width": 1200, "height": 1600}
      ]
    }
  }
}
```

Keys are backend kinds. Candidates are tried in order, at most four are read, and the first one
that downloads is used. A cover cached from another source is replaced once the new one has
downloaded; if the download fails the old cover stays. Nothing waits for the index when a cover is
already cached.

## What the app accepts

The document is untrusted. A candidate is dropped unless it is an `https` URL without credentials
or a port, on a host that is allowed **for that game in `nadare/core/artindex.py`**
(`ALLOWED_HOSTS`), with plausible `width`/`height` and an `orientation` of `wide` or `portrait`.
The index can therefore never send the app to a new host; adding a host is a code change.

## Updating it

1. Find a clean, official, high-resolution image (no event text, logo not clipped). Check it
   downloads (`curl -sIL URL` ends in `200` and `image/*`).
2. Put it first in the game's `cover` list; keep the old one behind it as a fallback.
3. `python3 scripts/check_art_index.py --index /path/to/nadare-index` should print `art index: ok`.

`docs/art-index.seed.json` is the document the first version shipped with (verified 2026-10-28);
`tests/test_artindex.py` checks that it passes the same validation as the app.

## Watching for rot

`scripts/check_art_index.py` runs from `scripts/refresh_index.sh` (skipped with
`NADARE_SKIP_CANARY=1`). A game whose candidates all fail turns the heartbeat "down" with a
`CANARY:` line; a dead first candidate with a working second one is only a note in the log.
