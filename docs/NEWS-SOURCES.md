# Official news sources

This document records which publisher news sources Nadare uses and what was
found while probing the remaining games. The goal is always the publisher's own
public feed, fetched at runtime and cached — never bundled and never scraped
from third-party sites by default.

Credit shown in the UI: `Artwork and news © their publishers. Nadare is not
affiliated.`

## Chaos Zero Nightmare (STOVE CWMS) — implemented

- Backend: `stove-czn` → source `src/nadare/backends/czn/news.py`.
- Endpoint (public, no auth, JSON):
  `GET https://api.onstove.com/cwms/v3.0/article_group/BOARD/{board}/article/list`
  with `content_yn=N&summary_yn=Y&sort_type_code=LATEST&translation_yn=N&page=1&size=6`
  and `Accept: application/json`. Envelope `{"code":0,"value":{"list":[…]}}`.
- Boards (channel `11034`): `142421` Updates, `142488` Known issues,
  `139238` Announcements, `139239` Developer notes.
- Accepted only when `channel_seq == 11034`, `board_seq` is one of the above and
  `user_info.channel_grade_code == "official"`.
- Fields used: `article_id`, `title`, `summary`, `create_datetime` (epoch ms),
  `media_thumbnail_url`.
- Article URL: `https://page.onstove.com/chaoszeronightmare/en/view/{article_id}`.
- Image host allowlist: `d2x8kymwjom7h7.cloudfront.net` (https, no userinfo/port).
- Boards are fetched in parallel; a partial failure sets `partial`, all-boards
  failure raises. Cache: `$XDG_CACHE_HOME/nadare/news/czn.json`, TTL 15 min,
  stale cache served offline.

## Arknights Global (Yostar) — implemented

The official site is a Vue SPA whose Axios instance uses `baseURL: "/"` and
prefixes every call with `/api/`. Its chunks (`assets/News-*.js`,
`assets/Footer-*.js`) reveal a clean, public JSON API.

- Backend: `arknights-yostar` → source `src/nadare/backends/arknights_news.py`.
- Endpoint (public, no auth, JSON):
  `GET https://arknights.global/api/resource/news/list?index=1&size=24&type=latest`
  with `Accept: application/json`.
  Envelope `{"code":0,"message":"ok","data":{"count":N,"rows":[…]}}`;
  anything else (non-zero `code`, missing `rows`) raises.
- News tabs (JSON, unused):
  `GET https://arknights.global/api/resource/news/type`
  → `{"code":0,"message":"ok","data":[{"label":"LATEST","value":"latest"}, …]}`
- Detail (JSON, unused):
  `GET https://arknights.global/api/resource/news/detail?id=<id>`
  → `{"code":0,"message":"ok","data":{…}}`
- Row fields used: `id` (int), `title`, `publishTime` (epoch ms), `type`
  (title-cased for the category), `smallImage` (preferred), `bigImage`,
  `link`.
- Article URL: `https://arknights.global/news/<id>` (an on-site `link` is used
  when it is on `arknights.global`; anything else is ignored).
- Summary: none is available in the list API, so it is left empty; detail
  pages are not fetched.
- Image host allowlist: `webusstatic.yo-star.com` (https, no userinfo/port).
- Newest-first, deduplicated, capped at 24 items. Cache:
  `$XDG_CACHE_HOME/nadare/news/arknights-yostar.json`, TTL 15 min, stale cache
  served offline. The hero keeps the default (newest with an image).

## Fate/Grand Order NA (Aniplex / Lasengle) — implemented

The NA webview publishes an Atom feed; each entry's link query names its
category and article path, and the article page carries the event banner.

- Backend: `fgo-lepton` → source `src/nadare/backends/fgo/news.py`.
- Feed (public, no auth, XML/Atom):
  `GET https://webview.fate-go.us/iframe/web/feed/news_update.xml`
  (namespace `http://www.w3.org/2005/Atom`, 10 `<entry>`).
- Entry fields used: `<title>`, `<updated>` (`MM.DD.YYYY`), and the first
  `<link href>`.
  - Category = the `category=` query value, title-cased (`NEWS` → `News`).
  - Article path = the `article=` query value (e.g.
    `/iframe/2026/1004_sugoroku_qbpu/`).
  - Item id = the article path as a slug (`2026-1004_sugoroku_qbpu`).
- Article page: `https://webview.fate-go.us` + the article path. Its event
  banner is the first `<img src>` under `/wp-content/uploads/` whose file name
  starts with `banner_` (`.png`/`.jpg`/`.jpeg`).
- At most the newest 8 article pages are fetched, in parallel; a failed article
  keeps the item without an image (`partial`).
- Public item URL: `https://fate-go.us/news/?category=…&article=…` (https).
- Image host allowlist: `webview.fate-go.us` (https, no userinfo/port).
- Hero category is `News`, so the hero is the newest event banner. Cache:
  `$XDG_CACHE_HOME/nadare/news/fgo-lepton.json`, TTL 15 min, stale cache served
  offline.
