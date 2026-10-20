# Artwork (covers, hero art, backgrounds)

Nadare shows per-profile art on library cards and in the game hero. If a profile
has no art it falls back to a deterministic gradient + monogram seeded from the
profile title, so the app never looks broken.

## How a profile points at art

Any of these profile keys may be a local image path (PNG/JPG/WebP); the first one
that exists is used:

```
cover            # a 2:3 portrait poster (the card + hero)
hero             # a wide key visual (hero banner)
thumb            # a small square tile
artwork          # generic fallback
custom_background  # user-picked image (Settings → Appearance)
```

Set them by hand in `config.json`, or via **Settings → Appearance → Background**
(which writes `custom_background`). Files are read at runtime; nothing is copied.

## Art provenance and precedence

Each profile key records where its file came from in an `art_source` mapping:

```json
{
  "cover": ".../game-cover.jpg",
  "hero": ".../official-hero.jpg",
  "art_source": {"cover": "thirdparty", "hero": "official"}
}
```

* `user` — chosen through Settings (Browse/Clear on the background field).
* `official` — fetched from the publisher (CZN news hero, Yostar launcher media).
* `thirdparty` — Steam CDN / SteamGridDB / AniList scrapes.

Precedence is **user > official > thirdparty > placeholder**, applied per key
and across keys, so an official wide hero is never hidden by an older
third-party portrait cover. An official fetch overwrites third-party or
unknown-source entries but never a `user` one; a third-party fetch never
overwrites an official or user image.

Profiles written before `art_source` existed have no mapping. The rule is:
`custom_background` is treated as `user`; any path whose filename looks like the
old fetcher's output (`*-cover.*`, `*-hero.*`, `*-logo.*` in the covers cache)
is treated as `thirdparty`; an image just written by an official fetch carries
the `official` tag that fetch recorded. Unknown keys default to `thirdparty`, so
official art may replace them.

## Yostar launcher media

The Yostar CI job publishes a `<tag>.media.json` next to the manifest and points
at it from `index.json` (see `ci/yostar-manifests/`). `yostar.load_media` reads
it (URL, local file, or the index's `media` entry) and `yostar.cache_media`
caches the background as `{slug}-official-hero.<ext>` and the operation banners
as `{slug}-official-banner-N.<ext>`. Only `https://*.yo-star.com` images are
fetched. The hero is recorded as `official`.

Official files never reuse the legacy third-party names (`{slug}-hero.<ext>`, …)
so a stale SteamGridDB scrape cannot shadow the publisher's art. Each official
file also gets a `{file}.src` sidecar holding its source URL; the cache is only
reused while the URL still matches, so a new launcher background is downloaded.

## Where launchers get "official" art

Other launchers are not magic — they use a small number of keyless or
API-key sources:

| Source | Keyless? | What you get | Notes |
| --- | --- | --- | --- |
| **Steam CDN** | yes | `header.jpg`, `library_600x900.jpg`, `logo.png`, `hero.jpg` | By appid: `https://cdn.cloudflare.steamstatic.com/steam/apps/<appid>/library_600x900.jpg`. Perfect for Steam games. |
| **SteamGridDB** | API key only | grids (2:3), heroes, logos, icons | Off by default. Only queried when `SGDB_API_KEY` (or a profile/config `sgdb_api_key`) is set, using the official v2 API with `Authorization: Bearer <key>`. The old keyless `/api/public` path has been removed. |
| **IGDB / TheGamesDB / RAWG** | no (key) | covers, artwork, metadata | Metadata-rich; heavier signup. |
| **Publisher press kits / official sites** | yes | high-res key art | Best quality, per-game. Nadare already uses the CZN news hero through a per-backend host allowlist. |
| **AniList GraphQL** | yes | anime key art (2:3) | Opt-in only (`NADARE_ART_THIRD_PARTY=1`). `POST https://graphql.anilist.co` `{Media(search:"Arknights", type:ANIME){coverImage{extraLarge}}}`. |
| **Fandom/Wikipedia** | yes | box art / logos (fair use) | `api.php?action=query&prop=pageimages&pilicense=any`. Low-res, licensing caveats. |

## Licensing

Do **not** commit copyrighted cover art to this repository. Either:

- fetch at runtime and cache under `$XDG_DATA_HOME/nadare/covers/`, or
- let the user pick a file (`custom_background`), or
- bundle art only when the publisher permits redistribution.

## Implemented

`core/artwork.py` resolves local art and can fetch it, cached under
`$XDG_DATA_HOME/nadare/covers/`:

1. **Steam CDN** when the profile has `steam_appid` (no key).
2. **SteamGridDB** with a key when `SGDB_API_KEY` (env) or a profile/config
   `sgdb_api_key` is set (matches by title). Off by default.
3. **AniList** only when `NADARE_ART_THIRD_PARTY=1`, for the profile `anilist`
   term or its title.

Third-party scraping is never used by default. Official publisher images are
downloaded through the single `artwork.download(url, slug, kind, allowed_hosts)`
helper, which enforces the backend's `official_image_hosts` allowlist (see
`docs/NEWS-SOURCES.md`). The CZN news hero is stored as
`{slug}-official-hero.<ext>` (with a `{file}.src` URL sidecar) and picked up
automatically by the library/game page. The credit string shown in
the UI is `Artwork and news © their publishers. Nadare is not affiliated.`

`fetch` downloads one kind; `fetch_all` (used by `fetch-art` and the Settings
button) fetches **cover**, **hero** and **logo** and records each on the profile.

Use **Settings → Appearance → Fetch art**, or:

```sh
nadare fetch-art -p Arknights      # one profile
nadare fetch-art --all             # every profile
```

On success the profile's `cover`, `hero` and `logo` keys are set to the cached
files. Failures are logged and ignored, never surfaced as errors.

## Test covers (local, not committed)

For local UI testing only:

```sh
# keyless anime key art
python3 - <<'PY'
from curl_cffi import requests; import pathlib
out = pathlib.Path.home()/".local/share/nadare/covers"; out.mkdir(parents=True, exist_ok=True)
Q = "query($s:String){Media(search:$s,type:ANIME){coverImage{extraLarge}}}"
for name, term in [("fgo","Fate/Grand Order"),("arknights","Arknights")]:
    u = requests.post("https://graphql.anilist.co", impersonate="firefox", timeout=30,
                      json={"query":Q,"variables":{"s":term}}).json()["data"]["Media"]["coverImage"]["extraLarge"]
    (out/f"{name}.jpg").write_bytes(requests.get(u, impersonate="firefox", timeout=60).content)
PY
```

Then set `custom_background` to the saved path.
