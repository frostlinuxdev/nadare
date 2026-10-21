# AI provenance and licensing

This document is permanent project record, not a disclaimer added after a
complaint. It describes how Nadare's code was produced, what we claim about it,
and what we explicitly do not claim. It is not legal advice.

## How the code was made

Most of Nadare's implementation code was written with an AI assistant (Claude),
working from human direction. The human maintainers designed the architecture,
chose the backends, specified behaviour, reviewed the output, and tested it
against real game installs. The model typed; people decided.

We do not publish a percentage, because there is no meaningful way to measure
it. "Lines the model typed" and "lines a human would have typed anyway" are not
the same thing, and a made-up ratio invites arguments instead of answers. The
honest statement is: **this is an AI-assisted codebase with human ownership of
the design, review and release process.**

## What humans actually wrote

The parts that matter for trusting the software are human-authored and
human-maintained, even where the model produced a first draft:

- architecture, backend boundaries, and the event/cancel model;
- the security decisions: secret handling, keyring rules, log masking, the
  refusal to fall back to plaintext for account credentials, and the journal
  recovery logic;
- the ToS and account-safety analysis in `docs/ACCOUNT-SAFETY.md`, `PRIVACY.md`
  and `SECURITY.md`, and the decision to state plainly that this software may
  put an account at risk;
- the packaging, release process, checksum/signature verification and the
  per-distro testing matrix;
- every bug that was found by running the thing on real hardware, including the
  edge cases the model got wrong and a human had to diagnose.

Review and testing are not decoration here. A launcher that mints game sessions
is a security-sensitive program; a human reading the auth path is the control
that makes the AI assistance acceptable.

## License: what the project actually is

The project is distributed under the **GNU General Public License, version 3
(`GPL-3.0-only`) as a combined work.** Individual files say
`GPL-3.0-or-later`; that per-file offer does not upgrade the package.

Two facts force v3-only:

| Constraint | Consequence |
| --- | --- |
| `src/nadare/backends/czn/auth.py` is `GPL-3.0-only` (inherited from czn-linux) | The combined work cannot be offered under "v3 or later", because we do not hold the right to grant a future v4 for that file. |
| PyQt6 is GPLv3 | A Python application importing PyQt6 is a combined work and must be GPLv3-compatible regardless of anything else. |

So, for packaging metadata:

```text
License: GPL-3.0-only
```

Source-level SPDX is mixed (`GPL-3.0-or-later` for most files, `GPL-3.0-only`
for `czn/auth.py`). `pyproject.toml` and the README said
`GPL-3.0-or-later` for the whole project at the time of writing; that is
inaccurate for the combined work. If you are packaging Nadare, use
`GPL-3.0-only` and do not "fix" it back.

## Copyright, and what we are not claiming

We are not claiming that the AI-generated portions of this codebase are our
original authored work. Under current US practice, material produced without
human authorship is not copyrightable, and we do not pretend otherwise.

What that means in practice:

- **We grant the GPL over the project as a whole** because we can grant the
  rights we hold (the human-authored parts, the selection and arrangement, and
  the rights Anthropic's terms assign to us over model output). We do not
  warrant that every individual line is protectable, or that we would be the
  one enforcing it.
- **Copyleft is not a revenue strategy here.** Nobody is going to get rich
  forking this. GPLv3 was chosen because it is compatible with PyQt6 and with
  the GPL-only code we build on, and because it matches the project's stance:
  the source ships, the game files do not.
- **If a file turns out to reproduce third-party code**, the project is
  GPLv3-compatible by construction, so the failure mode is a licensing mismatch,
  not silent infringement of an incompatible license. We will rewrite the
  offending file rather than argue about it. We cannot audit model weights, and
  we are not going to claim we did.

## Why not MIT or Apache-2.0

Not because we object to permissive licenses. Because shipping PyQt6 makes it
impossible: a distributed application that imports PyQt6 is a GPLv3 work. The
only clean path to a permissive license would be:

1. switch from PyQt6 to **PySide6** (LGPLv3, near-identical API), and
2. **rewrite** `backends/czn/auth.py` from scratch rather than continuing to
   port GPL-3.0-only code.

That is real work for a codebase whose copyright is already thin. It is not
planned.

## For contributors

- Disclose AI assistance in the PR description. It will not get a patch
  rejected by itself; undisclosed generated code that nobody has run will.
- Do not paste code you did not have the right to license. GPLv3-compatible
  only.
- Expect review to focus on behaviour, not style: does it run on real hardware,
  does it touch credentials correctly, does it fail safely.
- New files need an SPDX header, same as existing ones.

## For packagers and distributors

- `License: GPL-3.0-only` (see above). The AUR/RPM/Flatpak metadata must not say
  `or-later`.
- No game assets, credentials or provider secrets are shipped in this
  repository. Only the launcher code, its own icon and its own docs.
- Nadare is not affiliated with any game publisher; see `README.md` and
  `PRIVACY.md`.
- If your project's policy requires disclosure of AI-generated contributions,
  this file is that disclosure. Link it rather than paraphrasing it.

## Scope

This covers the code in this repository. The games, launchers, Proton builds
and Android stacks Nadare drives are separate works under their own licenses,
and Nadare claims nothing about them.
