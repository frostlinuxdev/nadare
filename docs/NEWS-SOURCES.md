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

## HoYoverse (Genshin, Star Rail, ZZZ, Honkai Impact 3rd) — implemented

- Backends: `direct-genshin`, `direct-hsr`, `direct-zzz`, `direct-hi3` → source
  `src/nadare/backends/hoyo_news.py`.
- `GET https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameContent?launcher_id=VYTpXlbWo8&game_id=<id>&language=en-us`
  → `data.content.posts[]` (id, type, title, link, date as `MM/DD` without a year) and
  `banners[]` (image url on `fastcdn.hoyoverse.com`, link). A banner whose link equals a post's
  link gives that post its image; other banners become "Featured" items.

## Scouting 2026-10-26: feeds found, not implemented yet

Probed with anonymous GET (and one read-only POST) only, nothing executed. "Verified" means the
endpoint was re-fetched by hand and returned real data; "reported" means a scouting worker saw it
and it was not re-checked. All of these are public and need no login.

### Verified

- **Wuthering Waves** (`direct-wuwa`). Static JSON on a media CDN, same shape as Punishing: Gray
  Raven below.
  - List: `GET https://hw-media-cdn-mingchao.kurogame.com/akiwebsite/website2.0/json/G152/en/ArticleMenu.json`
    (JSON array; `articleId`, `articleTitle`, `articleType` 57 News / 58 Notice / 59 Event,
    `startTime` as `YYYY-MM-DD HH:MM:SS` in CMS local time, `top`; **not sorted**: sort by
    `startTime`). Labels per locale from `.../en/MainMenu.json` → `articleType`.
  - Hero art: `MainMenu.json` → `pcTopPicture.coverImage` (1920×1080 PNG); banners:
    `picture[]` with `pictureType == 8`, `imgUrl`.
  - Article page: `https://wutheringwaves.kurogames.com/en/main/news/detail/<articleId>`.
    No thumbnails in the list. Hosts: `hw-media-cdn-mingchao.kurogame.com`,
    `wutheringwaves.kurogames.com`.
- **Punishing: Gray Raven** (`direct-pgr`). Same CMS, one locale:
  `GET https://media-cdn-zspms.kurogame.net/pnswebsite/website2.0/json/G167/ArticleMenu.json`
  (56 recent items) or `.../MainMenu.json` (all articles, hero `pcTopPicture.coverImage`,
  banners `picture[]` with `pictureType == 8`). Article JSON `.../article/<id>.json` carries
  `contentCover`. Article page `https://pgr.kurogame.net/news/<articleId>`. Hosts:
  `media-cdn-zspms.kurogame.net`, `pgr.kurogame.net`.
- **NIKKE** (`launcher-nikke`). The site's CMS answers an unsigned POST:
  `POST https://na-community.playerinfinite.com/api/gpts.information_feeds_svr.InformationFeedsSvr/GetContentByLabel`
  with headers `X-GameId: 16`, `X-AreaId: na`, `X-Source: pc_web`, `X-Language: en`,
  `Content-Type: application/json;charset=utf-8` and body
  `{"get_num":N,"offset":0,"primary_label_id":309,"secondary_label_id":496,"content_class":0}`
  (309/496 = news; 308/494 = home banners; label ids from `.../GetLabelList`). Items:
  `content_id`, `title`, `pub_timestamp` (unix seconds, as a string), `pic_urls[]`. Article page
  `https://nikke-en.com/newsdetail.html?content_id=<content_id>`. Hosts:
  `na-community.playerinfinite.com`, `na-nikke-aws.playerinfinite.com`.
- **Neverness to Everness** (`direct-nte`, `launcher-nte`). A static JavaScript file, not JSON
  (it has trailing commas): `GET https://nte.perfectworld.com/include/newsData<YYYYMMDD>.js`, whose
  name changes on each rebuild; read it from the `<script src>` in
  `https://nte.perfectworld.com/en/main.html`. `var newsdataObj = {"en": {"news": [...], "gamenews":
  [...], "gamebroad": [...], "gameevent": [...]}, ...}`, five headlines per column, each with
  `title`, `url` (relative, prefix `https://nte.perfectworld.com`), `time` (`YYYY-MM-DD`). No images
  in the list. Home banners: `https://www.perfectworld.com/public/commonData/gamesData/gameSwiper/nte-gameSwiper.js`.
- **Brown Dust 2** (`direct-bd2`). `GET https://webapi.browndust2.com/api/notices?locale=en-us&page=0&limit=N`
  → `{"items":[{"id","subject","category","publishedAt" (ISO 8601 UTC),"contentPreview"}],"totalCount",...}`.
  Article page `https://www.browndust2.com/en-us/news/view?id=<id>`. Host `webapi.browndust2.com`.
- **Arknights: Endfield** (`direct-endfield`). Plain JSON, no scraping needed:
  `GET https://web-news.gryphline.com/api/bulletin?lang=en-us&code=arknights_endfield_official&page=1&pageSize=N&tabs[]=notices`
  (also `events`, `news`) → `data.list[]` with `cid`, `tab`, `sticky`, `title`, `displayTime`
  (unix seconds), `cover` (on `web-static.hg-cdn.com`), `brief`. Article page
  `https://endfield.gryphline.com/en-us/news/<cid>`.

### Reported, not re-checked

- **Mongil: Star Dive**: no feed on the brand site; Netmarble's forum API
  `https://forum.netmarble.com/api/game/monster2/official/forum/stardive_gl/article/list?menuSeq=N&languageCd=en_US`
  returns articles for boards 6/7/8 (the NEWS menu was empty); paging parameter unresolved.
- **Stella Sora**: has a banners endpoint `GET https://stellasora.global/api/resource/news/banner`
  (`[{banner, url}]`) beside the news list Nadare already uses, and a Steam store page (app
  `4813850`, art from the Steam CDN).
- **Arknights**: the news list is implemented; the site's key visual is a static
  `webusstatic.yo-star.com/.../kv-bg-<hash>.png` (hash changes on a rebuild), so it is not a stable cover URL.
- **Chaos Zero Nightmare**: a stable brand share image exists per locale,
  `https://static-pubcomm.onstove.com/live/czn/brand/images/en/ogtag_1200_default.jpg`
  (1200×630); the official fan kit is at `https://chaoszeronightmare.onstove.com/en/fankit`.
- **Honkai Impact 3rd**: the launcher content API above gives banners (690×320) for a hero.

### Nothing usable

- **Girls' Frontline 2 (global, Haoplay)**: no news feed (the site's slider is hard-coded images);
  the launcher's `/flexible_config` has one promo banner and an update log only. The Chinese
  server has a JSON API (`gf2-web-preregister-api.sunborngame.com/website/...`) but in Chinese only.
- **Aether Gazer**: the site JS names `/api/news/list`, `/banner` and `/detail`, but the backend
  answered 503 to everything at scan time.
- **Kuro launchers** (Wuthering Waves, Punishing: Gray Raven): the launcher's own news panel needs
  a runtime `appKey` that is not in any public file; the website JSON above carries the same news.
- **NTE launcher**: no news endpoint found statically (its panel is a web view).
