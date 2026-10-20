# Yostar manifest generator

`generate.py` publishes **plain, normalized Yostar manifests** so the Nadare
client can install/update Arknights and Stella Sora directly, without shipping
Yostar's per-game signing salt in a GPL binary.

A scheduled CI job holds the salt, performs the
signed launcher calls, and commits JSON that anyone can consume.

## What it produces

For every configured game tag:

- `<GameTag>.json` — a manifest:

  ```json
  {
    "game_tag": "StellaSora_EN",
    "version": "7",
    "base_url": "https://cdn.yo-star.com/game/asset",
    "generated_at": "2026-01-01T00:00:00+00:00",
    "files": [{ "path": "StellaSora.exe", "size": 687488, "crc64": 123456789 }]
  }
  ```

  `crc64` is CRC-64/XZ (reflected ECMA-182, init/xorout all-ones), stored as a
  decimal integer. Yostar sends it as decimal or hex; both are normalized.

- `index.json` — a lookup for the client:

  ```json
  {
    "StellaSora_EN": {
      "version": "7",
      "url": "StellaSora_EN.json",
      "sha256": "<sha256 of the manifest file>"
    }
  }
  ```

## The three launcher calls

Base: `https://api-launcher-en.yo-star.com`.

1. `GET /api/launcher/game/config` → `data.game_latest_version`,
   `data.game_latest_file_path`.
2. `GET /api/launcher/game/config/json?version=..&file_path=..` → `data.url`.
3. `GET <data.url>` (public CDN, unsigned) → `{"source", "file": [...]}`.

Calls 1 and 2 are signed:

```
head = {"game_tag": tag, "time": unix_seconds, "version": launcher_version}
sign = md5(json.dumps(head, separators=(",", ":")) + data_str + salt)
Authorization = json.dumps({"head": head, "sign": sign}, separators=(",", ":"))
Accept: application/json, text/plain, */*
```

`data_str` is empty for these GETs. Errors come back as HTTP **200** with
`{"code": 403, "msg": "..."}`; any response without `data` is treated as a
failure and the code/message is surfaced.

## Configuration (environment)

| Variable | Meaning |
|---|---|
| `SALT_<TAG>` | per-game signing salt (required) |
| `VERSION_<TAG>` | launcher version string used in the signed `head` |

`<TAG>` is the game tag upper-cased with non-alphanumerics replaced by `_`, so
`StellaSora_EN` → `SALT_STELLASORA_EN`, `Arknights_EN` → `SALT_ARKNIGHTS_EN`.
Games without a salt are skipped with a warning; games without a version are
skipped too. A tag only counts as *configured* (and is expected in the index)
when **both** its salt and its version are present, so a half-rolled-out game
does not fail the job.

Run locally (offline is not possible for the real API; use a throw-away salt):

```sh
SALT_STELLASORA_EN=... VERSION_STELLASORA_EN=1.0.0 \
  python ci/yostar-manifests/generate.py --out dist/manifests
```

## Publishing (merge + keep-last-good)

`generate.py --merge-into <checkout>` writes the fresh manifests into `<out>` and
then merges them into an existing published checkout:

- every tag regenerated this run has its manifest file copied over and its
  `index.json` entry (version, url, sha256) refreshed;
- a tag that failed or was skipped this run keeps its previously published
  entry and file, so a flaky upstream can never empty the index;
- a file whose content differs only by `generated_at` is not rewritten;
- a parsable-but-empty upstream payload (missing/empty `file`) is treated as a
  generation failure, so it can never replace a good manifest with an empty one.

Exit status:

- `0` — at least one game was generated and merged. A newly added or
  bootstrap tag that could not be published only warns, so one broken game
  cannot wedge the whole job.
- `2` — no game could be generated at all (nothing to publish). The last-good
  published files are left on disk, and the workflow's `set -eu` stops the step
  before commit/push.
- `1` — a real error: the merged index would be empty, or the merge dropped a
  tag that had already been published (a fail-closed invariant check).

`scripts/publish_index.py` calls the same generator. It maps the dedicated `2`
("nothing to publish") to its own exit `3` and keeps the last-good published
files, but lets any other non-zero exit (a genuine crash or a dropped tag)
propagate so CI fails instead of silently absorbing it.

## Secrets to set in CI

- `SALT_STELLASORA_EN` — Stella Sora signing salt.
- `SALT_ARKNIGHTS_EN` — Arknights signing salt.
- `MANIFESTS_REPO` — git URL of the repository that hosts the published JSON.
- `MANIFESTS_TOKEN` — an HTTPS token allowed to clone/push `MANIFESTS_REPO`
  (optional). When set it is handed to git as a credential helper via the
  environment, so it never reaches argv or `published/.git/config`. If
  `MANIFESTS_REPO` is an **SSH** URL the URL's own deploy key is used and this
  token is ignored (the workflow warns on stderr).

`workflow.yml` in this directory is a **template** for that job (hourly cron +
manual dispatch). It is not wired to any real repository. It must be copied
into (and run from) the **Nadare client repo** — `actions/checkout` takes the
repo the workflow lives in, and the step runs `ci/yostar-manifests/generate.py`,
which imports `nadare.core.yostar` from that checkout's `src/`. The published
manifests repo is cloned separately into `published/` and only written to, never
used as the workflow's own checkout. The salt and the generated `Authorization`
header are never printed or written to the outputs.
