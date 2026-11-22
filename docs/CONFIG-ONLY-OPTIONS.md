# Config-only settings

These settings are real: Nadare reads them from a game's profile in `config.json`. Settings does
not show them because a normal user cannot know the right value and a wrong one breaks the
game. Testers and support can still set them by editing the profile (close Nadare first).
Saving in Settings never removes or changes them.

Example (`~/.config/nadare/config.json`, one game under `profiles`):

```json
"my-game": { "backend": "launcher-nikke", "installer_args": "/S" }
```

| key | what it does | used by |
| --- | --- | --- |
| `installer_url` | Download this installer instead of the publisher's current one | launcher games |
| `installer_sha256` | Checksum the installer must match | launcher games |
| `installer_args` | Extra arguments given to the installer | launcher games |
| `manifest_index` | Alternative download list index (Yostar games) | Arknights, Stella Sora |
| `manifest_file` | Alternative download list file (Yostar games) | Arknights, Stella Sora |
| `media_file` | Alternative media file list | Arknights |
| `kuro_token` | Kuro download token | Punishing Gray Raven, Wuthering Waves |
| `repo` | Folder of the lepton-fgo checkout | Fate/Grand Order |
| `mode` | Lepton installer mode | Fate/Grand Order |
| `size` | Lepton window size | Fate/Grand Order |
| `release` | Lepton release to install | Fate/Grand Order |
| `data_dir` | Where Lepton keeps game data | Fate/Grand Order |
| `logs_dir` | Where Lepton writes logs | Fate/Grand Order |
| `desktop_dir` | Where Lepton puts its desktop file | Fate/Grand Order |
| `cache` | Lepton cache folder | Fate/Grand Order |
| `adopt` | Use an existing Android context | Fate/Grand Order |
| `force_update` | Reinstall even when up to date | Fate/Grand Order |
| `purge_cache` | Delete the cache when uninstalling | Fate/Grand Order |
| `uninstall_all` | Remove every Lepton context on uninstall | Fate/Grand Order |
| `wine_binary` | Use this Wine when no runner is chosen for the game (an explicit runner wins) | plain Wine games; not Chaos Zero Nightmare |
| `umu_binary` | Use this umu-run instead of Nadare's own | Wine games that run through umu; not Chaos Zero Nightmare |
| `sandbox_share_pid` | Give the game this computer's process list instead of its own (PID namespace). Only for a game that cannot start without it | Sandboxed games |
| `sandbox_host_tmp` | Give the game this computer's `/tmp` instead of a private one. Only for a game that needs a file Nadare cannot put there | Sandboxed games |
| `exe_name` | The game or launcher executable inside the game folder; shown in Settings for Wine and Script games only | all but Wine and Script |
| `process_match` | Text used to find the running game; shown in Settings for Script games only, config-only for every other kind | all but Script |
| `stop_grace_seconds` | How long Stop asks the game to close before it is forced (default 8; `0` skips the request). Only for the games Nadare launches itself (launcher-based and direct); CZN stops at once | launcher-based and direct games |

Download workers is now in Settings (1–32). It caps the plan’s automatic worker count.
The download speed limit is global, in MB/s; 0 means no limit.
