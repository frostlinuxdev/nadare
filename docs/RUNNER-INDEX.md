# Signed runner pins (`runners.json`)

Maintainer notes. The public user documentation does not mention this file.

Nadare's recommended Proton runners used to be Python literals in
`src/nadare/core/runners.py`: every new tested build needed a Nadare commit, a cut and a
release (and, for the Flatpak, a new repository export). `runners.json` moves that pin to the
public index so a new tested build appears without a release.

## Where it lives

Signed `runners.json` and `runners.json.asc` sit at the root of the index the app already
reads for notices and catalog data (`core/hosting.py`, the public index by default). The
AppImage and the Flatpak use the same base URL. The file is fetched at most once every six
hours, in the background, by the existing runner prefetch.

## Format

The file that is signed is the body, with one entry per runner family:

```json
{
  "epoch": 9,
  "runners": {
    "dwproton": {
      "tag": "dwproton-11.0-19",
      "file": "dwproton-11.0-19-x86_64.tar.xz",
      "sha256": "…",
      "size": 342043716,
      "published_at": "2026-11-19T00:00:00Z",
      "tested": {
        "yes": true,
        "date": "2026-11-19",
        "report": "https://example.org/nadare/test-reports/dwproton-11.0-19"
      },
      "critical": false,
      "min_nadare": "0.1.0b25"
    }
  }
}
```

`file` is optional: it is the release asset name to download. Leave it out and Nadare derives
the name from its built-in pin. Give it when upstream changes its asset naming: a plain file
name that contains the entry's `tag` (no `/`, no `..`, no leading dot, at most 200 characters) ending in `.tar.gz`, `.tar.xz` or
`.tar.zst`. The maintainer script writes it automatically when the archive name is not the
derived one.

Families this version of Nadare does not know are skipped (with one log line); the families it
does know are still applied. `sha256` is the only digest trusted for a tested build: Nadare
never falls back to GitHub's `.sha512sum` for a pin that came from this file.

## Making a pin

1. Test the build in game and write the report.
2. Run the script from the source checkout:

   ```sh
   python scripts/make_runners_json.py dwproton-11.0-19 \
       /path/to/dwproton-11.0-19-x86_64.tar.xz --epoch 9 \
       --test-report https://example.org/nadare/test-reports/dwproton-11.0-19 \
       --output build/runners.json
   ```

   It hashes the archive and writes the JSON body. It does not sign or publish, and it does not
   talk to the network. `--output` is required. Useful options: `--family ge|cachyos|dwproton`
   (default `dwproton`), `--published-at`, `--tested-date`, `--critical`, `--min-nadare`,
   `--untested`. When the archive name is not the one Nadare derives from its built-in pin, the
   script records it in the entry's `file` field.
3. Sign the body on the signing machine with the Nadare release key (the same key that signs
   the AppImage), by hand:

   ```sh
   gpg --armor --detach-sign --local-user 0F536F244C481212A3095B521E05855E99D918D5 \
       --output runners.json.asc runners.json
   ```

   The script prints this command. Keep the body byte-for-byte as written: the signature covers
   those bytes, not a reformatted copy.
4. Copy `runners.json` and `runners.json.asc` to the index root and publish the index only on
   the user's go. There is no automatic publisher for this file.
5. If the pin should only reach newer apps, pass `--min-nadare` with the first Nadare version
   whose built-in floor is not newer than the new tag. `min_nadare` is compared with the running
   app's version; older apps keep their own pin.

## Keeping other families, and signed rollbacks

Pass the body that is live now with `--previous`: the script keeps its other families and,
without `--epoch`, uses its epoch plus one. The epoch must go up for every publication, because
a file at the same epoch with different bytes is refused.

```sh
python scripts/make_runners_json.py GE-Proton11-8 /path/to/GE-Proton11-8-x86_64.tar.gz --family ge \
    --test-report https://example.org/nadare/test-reports/GE-Proton11-8 \
    --previous build/runners.json --output build/runners.json
```

A signed rollback goes back to an earlier build (never below the built-in pin). Add
`--rollback`; the script records the tag it replaces in `previous_tag` and still raises the
epoch:

```sh
python scripts/make_runners_json.py dwproton-11.0-17 \
    /path/to/dwproton-11.0-17-x86_64.tar.xz --test-report https://example.org/nadare/test-reports/dwproton-11.0-17 \
    --previous build/runners.json --rollback --output build/runners.json
```

One publication changes one family; a body with several families is fine (and is what
`--previous` keeps together).

## What Nadare accepts

- **Built-in pin is the floor.** With no index, no network, a failed fetch, or a bad signature,
  Nadare keeps the pin compiled into the app. An old app keeps working exactly as before.
- **Any other problem ignores the whole file.** A bad or missing signature, a malformed entry
  of a known family, a duplicate field, or an entry older than the built-in pin rejects the
  whole file; Nadare logs one warning and retries no sooner than the next six-hour refresh.
  An entry for a family this version does not know is skipped, with one log line, and the
  known families are still applied.
- **Newer only.** A tag older than the current pin is refused, unless a signed rollback has a
  higher `epoch` and names the tag it replaces in `previous_tag`. Replaying an older manifest
  (lower `epoch`) is refused. Omitting an accepted eligible family or making it ineligible
  rejects the whole file; use an explicit rollback to withdraw a pin. An unchanged tag must
  keep its accepted digest and size.
- **One visible line.** A moved pin is logged once and shown once as "Recommended runner
  updated to <tag>.", with a button to Settings. The download is the existing background
  runner prefetch (#1425), not a new one.
- **Last accepted pin survives a failure.** A bad new file or an outage never drops a user
  back to the built-in floor; the last accepted signed body stays in use.
- **`min_nadare`** hides a pin from older apps, which then keep their built-in pin.
- **`critical` only offers.** It never edits a profile and never touches a running game. Games
  whose settings point at the previous recommended build get the usual per-game Settings hint;
  games that name their own runner are left alone.
- **`tested: {"yes": false}`** keeps the built-in pin for that family; a report link is only
  required for a tested build.

## Verifying by hand

```sh
gpg --verify runners.json.asc runners.json
```

The key is the release key in `packaging/signing/nadare-release-signing.asc`
(fingerprint `0F53 6F24 4C48 1212 A309  5B52 1E05 855E 99D9 18D5`). Nadare checks the same
detached signature in `core/releasesig.py` before it reads any field.
