# Account safety — design brief

Status: **CZN (section 1) implemented 2026-10-12; FGO (section 2) still a brief.**
The section 4 tests for secrets/events/CZN are in place. Written 2026-10-12 as a
test-first task list for the next implementation pass. It builds on the account rules already in lepton-fgo
(`lepton/account-notice.txt`, `lepton-fgo backup`).

Guiding rule: **anything that lets someone become you in the game is a
credential.** For CZN that is the STOVE refresh token. For FGO it is the game's
app data, which holds the login.
Both get the same treatment: minimum kept, never in plaintext we control, never
in logs, events or bug reports.

---

## 1. CZN (STOVE)

What we do:

1. **Remember me is opt-in, off by default.** Without it, tokens live in memory
   for the process and are gone on exit; the next start needs a QR login.
2. **Persist only the refresh token + member number** (the account identifier),
   and only in the OS keyring. Never the access token, game token or password.
3. **No plaintext fallback for tokens.** If the keyring is unavailable or a write
   fails, keep the session in memory and show a notice ("Remember me is not
   available: no system keyring"). `SecretStore(allow_fallback=False)` for
   tokens; the `0600` fallback may stay for non-account data only.
   `NADARE_NO_KEYRING=1` then means "never remember", not "write plaintext".
4. **Identity continuity.** On restore, refresh, then compare the returned
   member number with the stored one. Mismatch → delete the stored credential,
   require a new QR login, tell the user why.
5. **One restore attempt per start, no retry loops.** A network error leaves the
   user logged out with a "Retry" button. Repeated automatic refreshes look like
   a bot and burn the refresh chain.
6. **Tokens never reach the GUI.** Backend → GUI `Event`s carry the QR image/URL,
   state and public profile info only. Add a test that fails if any emitted
   event payload contains a value equal to a token.
7. **Turning Remember me off** deletes the keyring entry immediately but keeps
   the in-memory session. **Log out** deletes it and clears memory.

Nadare-specific constraint — the stub needs the game session on disk:

- `backends/czn/backend.py` writes `session.json` (access token, ~6 h) and passes
  its Windows path to the PCSDK stub. Keep that, but make it **ephemeral**:
  create it `0600` right before launch **next to the stub executable** (the
  proven czn-linux `tools/session.json` layout; `$XDG_RUNTIME_DIR` is not
  mounted into the umu/Steam-Runtime sandbox, so a session there is invisible to
  the game), and delete it when the game exits or on the next start if a
  crash left it behind. It must not live in the profile/data dir between runs.
- `czn_auth.py` today stores its own auth file as `0600` plaintext. Route its
  refresh token through the keyring path above (or have the backend own storage
  and pass the token in via stdin/env, never argv).

**Single owner of a session.** A new STOVE login elsewhere invalidates the
older refresh chain (`44010`). Before launch, if another client (official STOVE,
another launcher, a second Nadare profile on the same account) is detected or the refresh
returns `44010`, stop and explain instead of retrying.

## 2. FGO (lepton-fgo)

FGO's game data on disk is sensitive: treat the whole FGO app-data area like a
credential. Implementation details and the threat analysis are kept in
private developer notes, not in this repository.

Rules for Nadare:

1. **First-run account notice** when an FGO profile is created: show lepton's
   `account-notice.txt` text (link an Aniplex Online account where available,
   otherwise save a transfer number) and require an acknowledgement checkbox
   before the first launch. Store only that it was acknowledged.
2. **Automatic encrypted backup before update / repair / uninstall**, via
   `lepton-fgo backup <ctx>` (always encrypted). Failure aborts the operation
   (user can override). Never produce a plaintext copy, never in `/tmp`.
3. **Integrity check around update/repair**: if the game data changed when it
   should not have, stop and warn; never overwrite or prune the last good
   backup in that case.
4. **No export/import or "copy to another install" feature.** Backups restore
   only into the context they came from. Moving devices uses the game's own
   transfer number or account linking.
5. **Bug reports / log export** exclude the FGO data area; the log masker
   masks paths under it.
6. **Protection at rest** (encryption while the game is not running, an access
   warning) is planned in lepton-fgo; the launcher only calls it.

## 3. Both games

- README + first-run screen: unofficial, may be against the game's terms,
  ban risk exists, do not use an account you cannot afford to lose.
- `SECURITY.md`: what to do if an account may be compromised (CZN: log in
  again to invalidate the old session chain, change password; FGO: contact
  support, re-secure with the game's own transfer/linking options).

### Netmarble (Mongil: Star Dive)

The game is given `NMAUTH_TOKEN` on its command line, so while it runs the
launcher token is visible in `/proc/<pid>/cmdline` to any process running as
the same user. Whether the game would read it from the environment alone has
not been tested, so argv is still used. Treat
this as a known exposure: it is limited to the local account and the token's
lifetime, but it is not secret from same-user processes. A future launcher
update should retest an environment-only handoff.

## GUI wiring

- *Settings → Account → Remember me*: per-profile toggle, shown only for backends
  with `GameBackend.supports_remember_me` (currently CZN). Saving starts a
  `set_remember` worker, so turning it **off** deletes the keyring entry
  immediately while the in-memory session survives.
- *Log out* on the game page: a `logout` worker clears the keyring entry, the
  on-disk cache and the process-wide in-memory session.
- Both run through `BackendController` (`OPERATIONS` includes `set_remember` and
  `logout`); the GUI never calls a backend from the main thread.

## 4. Tests to write first

- `secrets`: tokens refuse plaintext fallback; keyring-absent → in-memory only +
  notice; delete on "remember off"; `NADARE_NO_KEYRING=1` never writes a file.
- `czn`: persisted keyring blob contains only refresh token + member number;
  member-number mismatch → credential deleted + login required; restore tried
  exactly once; `session.json` created `0600` next to the stub executable and
  removed after the (mocked) game exits and on stale-file startup (only while
  not running). The host-only credential handoff stays in
  `$XDG_RUNTIME_DIR/nadare/<profile>/`.
- `events`: no emitted event contains a token value (inject a sentinel token).
- `fgo`: update/repair calls backup first and aborts on backup failure;
  unexpected data change → warning + last backup protected; acknowledgement
  gate blocks first launch.
- `logging`: masking covers tokens and FGO data paths.
