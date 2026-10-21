# Security

Nadare stands in for official launchers and holds **account sessions**. Treat a
leak of those tokens as seriously as a leaked password.

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use GitHub's
**private vulnerability reporting** on the Nadare repository (Security tab → "Report a vulnerability").

Please include:

- Nadare version (`nadare --version`) and how you installed it (source or AppImage).
- OS, kernel, GPU/driver, and the game + backend involved.
- A minimal reproduction and the impact you believe it has.
- Redacted log lines if relevant.

We will acknowledge as soon as we can. This is a volunteer project with no
guaranteed response time. Please give us a reasonable window to ship a fix
before disclosing publicly.

## Never post these

Even in a *private* report, avoid sending live secrets unless we ask:

- QR codes (any screenshot of a login QR — anyone who sees it can log in).
- Access / refresh / launcher tokens, `session.json`, czn auth state.
- `~/.config/nadare/secrets.json` or keyring contents.
- Raw authentication traffic, cookie jars, proxy captures with `Authorization`
  headers.
- Registry exports or full command lines containing token arguments.

Redact first: local paths can also be sensitive. If a log is needed, include the
lines around the failure, not the whole file.

Note that Nadare's logger masks secret-looking values on the way in, but **not
everything is a 24+ character blob** and masks are a safety net, not a guarantee.
Review logs yourself before sharing.

## If you think a token leaked

1. **CZN:** run `nadare login` again to mint a new session; the old game token is
   retired by the server. If you suspect the rotating STOVE refresh token was
   exposed, log in fresh and stop using the old state.
2. **FGO:** change your account's recovery method from inside the game
   (My Room → Account Linking / Issue Transfer Number) and contact the game's
   support. Full-disk encryption limits the blast radius of a stolen disk.
3. Delete `~/.config/nadare/secrets.json` and clear the `nadare-launcher`
   keyring entries, then change any account credentials you reused elsewhere.

## Scope

In scope:

- Nadare's own code: token handling, masking, file permissions, journal path
  validation, download verification, subprocess handling.
- Packaging that ships something other than the source (verifiable artifacts,
  checksums).
- Cases where Nadare logs or transmits a secret it should not.

Out of scope (report upstream):

- Vulnerabilities in the games, in STOVE, in `lepton-fgo`, in Wine/Proton, umu,
  Waydroid, or in the NVIDIA/Venus stack.
- Account bans or Terms of Service enforcement. Running these games this way may
  violate their ToS; that is a risk you accept, not a vulnerability.
- Anti-cheat behaviour. Nadare does not modify anti-cheat.

## Supported versions

Nadare is pre-1.0. Security fixes land on `main` and in the newest release.
There is no long-term support branch yet.
