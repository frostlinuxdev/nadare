# The notices feed

For the Store's optional `catalog/upcoming.json` feed, see [The coming-soon feed](UPCOMING.md).

Nadare reads `<index base>/notices.json` once per start (cached six hours, silent on any
failure; turned off by *Show safety and service notices*, which is separate from update checks). It exists so a broken game or a
needed update can be announced without a release, including to builds that cannot be changed.

```json
{
  "notices": [
    {
      "id": "endfield-2026-10",
      "text": "Arknights: Endfield installs are failing right now; a fix is coming.",
      "severity": "warn",
      "below": "0.1.0b11",
      "kinds": ["direct-endfield"],
      "link": "https://github.com/frostlinuxdev/nadare/issues/1"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | Required, `[A-Za-z0-9._-]`, up to 64 characters. Dismissing a notice remembers its id, so change the id to show a changed message again. |
| `text` | Required, plain text, up to 280 characters (control characters are stripped). |
| `severity` | `info` (default), `warn` or `block`. The most severe applicable notice is shown; `block` has no Later button. It is only a banner: nothing is ever disabled. |
| `below` | Optional version. The notice applies only to Nadare builds older than this. |
| `kinds` | Optional list of game kinds (for example `direct-endfield`). Applies only to users who have a profile of one of them. |
| `link` | Optional. Shown as a *Learn more* button the user must click. Only `https://github.com/...`, `https://frostlinuxdev.github.io/...` or the host the feed came from are accepted. |

Limits: at most 10 notices and 64 KB; anything malformed is skipped. The feed carries text only:
no code, nothing is opened or downloaded automatically.

## Holds

A hold is the stronger version of a notice: it asks before a game marked dangerous starts.
The list is `<index base>/holds.json`, an envelope signed with the same release key as Nadare's
updates and the runner index:

```json
{"body": "{\"version\": 1, \"serial\": 1, \"holds\": [{\"id\": \"nikke-2026-11\", \"kinds\": [\"launcher-nikke\"], \"below\": null, \"reason\": \"Reported compromise of the publisher's launcher.\", \"link\": \"https://github.com/frostlinuxdev/nadare/issues/805\"}]}", "signature": "-----BEGIN PGP SIGNATURE-----\n...\n-----END PGP SIGNATURE-----\n"}
```

The signature covers the exact text in `body` and is checked before the body is read. A missing
or bad signature, or an unreachable index, never blocks anything: Nadare keeps the last signed
list it verified, and starts normally when there is none.

`serial` is required and must be a whole number of at least 1. Increase it with every published
change to the list. Nadare saves the highest serial it accepted, together with the hash of that
body and the signed body itself, in `$XDG_STATE_HOME/nadare/holds-serial.json`. It refuses a
list with a lower serial, and one with the same serial but a different body, so an old signed
copy cannot be replayed to switch a hold off. The saved signed body is checked again and is the
fallback when the index copy could not be saved, so an accepted hold survives a failed write. A
missing or unreadable saved value is treated as "nothing accepted yet".

Nadare reads and writes that record under a lock, so two copies of Nadare started at the same
time cannot move the saved serial backwards.

The body has three fields: `version` (must be `1`), `serial` (see above), and `holds` (a list
of entries). Each entry has:

| Field | Meaning |
|---|---|
| `id` | Required, `[A-Za-z0-9._-]`, up to 64 characters. Accepting "start anyway" remembers this id for that profile; a new id asks again. |
| `kinds` | Required list of backend kinds the hold covers, for example `launcher-nikke`. |
| `below` | Optional Nadare version. The hold applies only to Nadare builds older than this. |
| `reason` | Required plain text, up to 280 characters (control characters are stripped). |
| `link` | Optional. Only `https://github.com/...`, `https://frostlinuxdev.github.io/...` or the host the feed came from are accepted. |

Limits: at most 20 holds and 64 KB; a malformed entry is skipped without dropping the rest.

What a hold does:

- The first time a held game is started, Nadare shows the reason and link. **I understand, start
  anyway** remembers that hold id for that profile. If the list carries more than one hold for
  the same game, each is shown in turn, and Play starts once all of them are accepted.
- `nadare launch` prints the reason and link and exits with code **13** unless `--ignore-hold`
  is passed.
- *Settings → Ask before starting a game marked dangerous* (on by default) turns
  the whole check off.

What a hold does **not** do: it never disables install, update, repair, remove or Stop, never
deletes or changes files, and never stops a game that is already running. It is a speed bump,
not a kill switch. If the list cannot be checked, launches work as before.
