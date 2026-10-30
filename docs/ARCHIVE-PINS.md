# Archive block pins

A **block pin** is a published list of SHA-256 hashes, one per fixed-size block of
each part of an archive. An index node computes it from the archive the publisher
served and publishes it with the catalog. When a streamed install finds a pin for
an archive, it checks every block it fetches *before* using it. A CDN serving
different bytes is therefore refused even if the zip/7z's own CRCs are internally
consistent. Without a matching pin, installs behave exactly as before.

Pins live in the catalog index at `catalog/archive-pins/<game_tag>.json`. The
default block size is 1 MiB (`PIN_BLOCK`); any power of two from 4 KiB to 16 MiB
is accepted.

## Document schema

```json
{
  "schema": 1,
  "archives": {
    "game-archive": {
      "block_size": 1048576,
      "publisher_md5": "0123456789abcdef0123456789abcdef",
      "parts": [
        {
          "url": "https://cdn.example/game.zip",
          "size": 123456789,
          "blocks": "base64 of the concatenated 32-byte SHA-256 digests"
        }
      ]
    }
  }
}
```

`publisher_md5` is optional. `blocks` decodes to exactly
`32 * ceil(size / block_size)` bytes; the final block of a part may be short.
The document is deterministic: archive names are sorted and no timestamp is
added unless a generator supplies `generated_at`.

## Matching and failure

A pin applies only when the archive has the same number of parts, and every part
has the same URL and size. If both the pin and the catalog declare a publisher
MD5, they must agree (case-insensitively).

On a block mismatch the reader refetches that range once. If the second answer is
also wrong, the install stops with a `DirectError` naming the part and block; no
unverified block is cached or installed. The `.done` record of a pin-verified
install says `"verified": "pin"` and records `pin_block_size` instead of claiming
the archive's own member CRCs were the only check.

Pins are optional: an unavailable or malformed pin document is ignored with a
warning, and the install falls back to the normal streamed path.

## Behaviour notes

- **Fail closed.** When an archive has a matching pin, Nadare never falls back to an unchecked download: a server that refuses ranged reads, or any block that does not match (after one refetch), stops the install with a message.
- **Sources without a size.** Some sources (Aether Gazer, GFL2) do not know the archive size. The pin supplies it; a wrong size on the server fails the block checks.
- **Record.** The `.done` record stores `verified: "pin"` and a `pin_fingerprint` of the table, so a corrected pin makes the installed archive verify again (only the blocks actually read are checked; unchanged local members are still skipped by their size and CRC).
- **Caching.** Pin documents are fetched once per two minutes per game, and a saved copy is used for at most two days when no mirror answers, so a retracted pin does not linger.

## Publishing pins (one node)

`scripts/publish_pins.py` builds the pin document. Run it on a single index node, not
from the 15-minute refresh loop, because a large archive takes minutes to stream:

```
python scripts/publish_pins.py --index ../nadare-index --game aethergazer --game mongil
```

It streams each archive once, hashing every block and the whole file without keeping
it on disk, and publishes only when the md5 matches the publisher's. Everything is
checked before the first byte is streamed: the plan has archives, each has a publisher
md5 and a URL a client would accept, the block size is valid and the pin directory is
writable. A mismatch, a missing md5 or a failed download leaves the previous pin
untouched.

- **One document per game, current plan only.** The file holds exactly the archives of
  the plan the publisher serves now, so it cannot grow past the client's 16 MiB cap.
  An older version's pin disappears when a new version is published.
- **Reuse.** A pin is reused (no download) only when URL, size, md5, block size and a
  recorded publisher md5 all match, so re-running is cheap.
- **Damaged files.** A document that does not parse is regenerated with a warning on
  stderr. One that cannot be read (permissions, I/O) stops the run and is left alone.
- **Limits.** `--deadline` (default 2 hours) is one budget for the whole run, `--max-bytes`
  (default 128 GiB) stops an endless stream, and a deadline that expires while the file is
  being written does not fail a publish that is already complete.
- **Lock and exit status.** A per-game lock under `<index>/.locks/` keeps two runs apart;
  a held lock skips the game. Exit status: 0 published, 3 nothing to publish, 1 error.
- Single-part archives only for now (Aether Gazer, Mongil); Endfield's split parts need
  their own job.

## Running it on the nodes

`scripts/publish_pins_node.sh` wraps the publisher for a systemd timer: it pulls the index,
publishes, commits only `catalog/archive-pins/`, pushes, writes a status file and pings a
heartbeat. Unit files are in `packaging/systemd/nadare-pin-publisher.{service,timer}`.

- **Its own checkout.** Do not share the index checkout of `refresh_index.sh`: that job stages
  all of `catalog/` and would commit the pins under its own message. A node needs only
  `scripts/` and `src/` of this repo plus a clone of the index (`~/nadare-pins/`).
- **Settings** go in `/etc/nadare-pin-publisher.env` (mode 600): `NADARE_PIN_GAMES`,
  `NADARE_PIN_DEADLINE`, `NADARE_PIN_PUSH=0` (commit but do not push), and
  `NADARE_PIN_HEARTBEAT_URL`, an Uptime Kuma push URL. The URL is a secret and is never
  printed or written to the status file.
- **Status file** (`~/nadare-pins/status.json`): `ok`, `result` (`published`, `committed`,
  `unchanged` or `failed`), `message`, `games`, `commit`, `host`, `time`.
- **Heartbeat.** One ping per run, `down` with the reason when the run failed. Make the Kuma
  monitor expect a ping about every hour plus slack, so a stopped timer alerts too.
- **Partial failures.** Each game's file is written atomically, so pins published before a
  failure are still committed and pushed; the run then reports `failed`.
- **Fallback node.** Run the same unit on a second node with a different boot offset. The job
  is idempotent: the second node pulls the pushed document, finds the pin current and does not
  stream anything.
- **Switching it off for everyone.** Publish the empty document
  `{"schema":1,"archives":{}}` for the game (do not delete the file: a missing file makes the
  client fall back to its saved copy for up to two days).

Until the node has published a pin for a new version, installs of that version run
the normal streamed path (member CRCs only), as before.
