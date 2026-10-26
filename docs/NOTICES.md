# The notices feed

Nadare reads `<index base>/notices.json` once per start (cached six hours, silent on any
failure; turned off by *Check for launcher and game updates*). It exists so a broken game or a
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
