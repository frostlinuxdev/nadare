# The coming-soon feed

The Store ends with a small **Coming soon** row. It exists so a game can be announced
between releases: a new game needs a Nadare release, a coming-soon card does not.

The row is fed from `<index base>/catalog/upcoming.json` in the `nadare-index` repository.
Nadare reads it once per run (cached six hours, silent on any failure) and shows at most
eight cards. A card has no Install button: only its *Official page* link opens a browser.

```json
{
  "upcoming": [
    {
      "title": "Silver Palace",
      "publisher": "Silver Studio",
      "when": "Q1 2027",
      "art": "https://cdn2.unrealengine.com/key-art.jpg",
      "link": "https://silver.example/"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `title` | Required, plain text, up to 160 characters (control characters are stripped). Titles are compared case-insensitively. |
| `publisher` | Required, plain text. |
| `when` | Required, free text such as `Q1 2027` or `To be announced`. It is not a date and nothing is computed from it. |
| `art` | Optional. An `https` image on a host already allowed for cover art in `nadare/core/artindex.py` (`ALLOWED_HOSTS`). Any other host is dropped and the card uses a placeholder; the index can never widen that list. |
| `link` | Optional. The game's official page, `https` only, without credentials or a port. Shown as *Official page*; a missing or invalid link leaves the button off. |

Limits: the document may be at most 64 KB on the wire and after parsing, at most eight
cards are shown, a malformed entry is skipped, and a repeated title is shown once. The
document carries text and an image URL only: nothing is installed, run or opened
automatically.

A card is hidden as soon as its `title` matches a game in the real catalog (any build of
it, whether or not the current search or tier filter would show that game). Removing the
entry from the document hides the card too. When the file is missing, empty or offline
with no cached copy, the row is not shown and nothing is reported to the user.

`tests/fixtures/upcoming.json` is a sample document; `tests/test_upcoming.py` and
`tests/test_upcoming_gui.py` cover the format and the row.
