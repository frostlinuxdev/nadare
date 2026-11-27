# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Sandbox support shared by the Wine backends: wrap a launch with the home hidden.

The backend supplies where its prefix and game folder are; this mixin decides whether the
Sandbox is wanted, checks that bubblewrap can run here, and builds the wrapped command.
"""

from __future__ import annotations

import os
import tempfile
import urllib.parse
from collections.abc import Iterable
from pathlib import Path

from . import gamesandbox, hostlibs, linkrelay, paths, sandbox
from .backends import BackendError, Option
from .config import as_bool, profile_value

#: Last-resort per-game exemptions from the Sandbox's private /tmp and PID namespace.
#: Kept out of Settings (config-only): a wrong value gives back isolation the Sandbox
#: exists for, and only a broken game needs it.
SANDBOX_EXEMPTION_OPTIONS = (
    Option(
        "sandbox_share_pid",
        "Share the game's process list with this computer",
        "bool",
        "Turns off the game's private process list (PID namespace). Only for a game "
        "that cannot start without seeing this computer's processes. Config file only.",
        default=False,
        advanced=True,
    ),
    Option(
        "sandbox_host_tmp",
        "Let the game use this computer's temporary folder",
        "bool",
        "Turns off the game's private temporary folder (/tmp). Only for a game that "
        "needs a file Nadare cannot put there. Config file only.",
        default=False,
        advanced=True,
    ),
)


def _broad_message(exc: Exception) -> str:
    """A too-broad game folder stops the launch, on by default or switched on (2026-11-11).

    It used to run the game unprotected with only a log line when the Sandbox was on by default.
    """
    return (
        f"Sandbox cannot hide your home folder from this game: {exc}. Move the game into a "
        "library (⋯ → Move to another folder…), or turn Sandbox off for this game on its page."
    )


def _umu_launcher_end(argv: list[str]) -> int | None:
    """The index of the argument that starts umu, when ``argv`` launches umu, else ``None``.

    Only the launcher position counts: ``umu-run ...``, or Nadare's own hidden wrapper
    (``nadare --umu-run ...`` frozen, ``python -m nadare --umu-run ...`` from source). A game
    argument that happens to say ``umu-run`` must not run the game outside the Sandbox.
    """
    if not argv:
        return None
    if Path(argv[0]).name == "umu-run":
        return 0
    for lead in (1, 3):
        if argv[lead : lead + 1] == ["--umu-run"] and (lead == 1 or argv[1:3] == ["-m", "nadare"]):
            return lead
    return None


class SandboxMixin:
    """The Sandbox methods, for backends that can run a game under bubblewrap."""

    _link_relay: linkrelay.LinkRelay | None = None

    #: Whether the last :meth:`_secure_wrap` really put its command in the Sandbox;
    #: ``None`` before the first call. The Sandbox can be on and still be skipped (no
    #: bubblewrap here), and Stop must then treat the game as the unsandboxed one it is.
    _sandbox_wrapped: bool | None = None

    #: The command and environment as they run inside the last bubblewrap Sandbox, so Stop
    #: can run its ``taskkill`` there (#1766); ``None`` when there is none to join.
    _sandbox_inside: tuple[list[str], dict[str, str]] | None = None

    def _sandbox_prefix(self) -> Path:
        """The Wine prefix the Sandbox binds writable."""
        raise NotImplementedError

    def _sandbox_game_dir(self) -> Path | None:
        """The game folder the Sandbox binds writable, or ``None``."""
        raise NotImplementedError

    def secure_mode_enabled(self) -> bool:
        return gamesandbox.wanted(
            self.ctx.profile,
            supported=self.supports_secure_mode,
            default=self.sandbox_default_on,
            required=bool(getattr(self, "sandbox_required", False)),
        )

    def _sandbox_insisted(self) -> bool:
        """Switched on by the player, or required by the game: no unprotected fallback."""
        return gamesandbox.choice(self.ctx.profile) is True or bool(
            getattr(self, "sandbox_required", False)
        )

    def _sandbox_exemptions(self) -> dict[str, bool]:
        """The per-game opt-outs from the private /tmp and PID namespace (all off)."""
        profile = self.ctx.profile
        return {
            "share_pid": bool(as_bool(profile_value(profile, "sandbox_share_pid"), False)),
            "host_tmp": bool(as_bool(profile_value(profile, "sandbox_host_tmp"), False)),
        }

    def _sandbox_check(self, subject: str) -> gamesandbox.Located:
        """The bubblewrap the Sandbox will use here, or why there is none.

        Only the "can it run here" part: an explicit Sandbox on raises instead of falling
        back. A missing bubblewrap is still returned (``binary`` is ``None``) so the caller
        can say why the ``subject`` runs without it when the Sandbox is only on by default.
        """
        explicit = self._sandbox_insisted()
        located = gamesandbox.locate()
        if located.binary is None and explicit:
            if not located.found:
                command = gamesandbox.install_hint(hostlibs.distro_family())
                raise BackendError(
                    "Sandbox needs bubblewrap and Nadare could not find a copy. It comes "
                    "with the game runtime Nadare downloads the first time a game runs; or "
                    f"install it with: {command}. Turn Sandbox off to play without it."
                )
            if located.untrusted:
                raise BackendError(
                    "Sandbox did not start: the game runtime was updated and Nadare has not "
                    "been told to trust its new bubblewrap yet. Restart Nadare and answer "
                    '"Trust the new copy", or turn Sandbox off to play without it.'
                )
            raise BackendError(
                f"Sandbox cannot start here ({located.why}). On Ubuntu 24.04 and newer the "
                "system blocks it; `nadare doctor` shows the fix and docs/SECURE-MODE.md "
                "explains it. Turn Sandbox off to play without it."
            )
        return located

    def _secure_wrap(
        self,
        argv: list[str],
        env: dict[str, str],
        *,
        subject: str = "game",
        read_only: Iterable[Path] = (),
        links: bool = True,
    ) -> tuple[list[str], dict[str, str]]:
        """Run the program inside bubblewrap with the home hidden, when the Sandbox is on.

        Play, and a publisher's installer (``subject="installer"``), go through here; prefix
        setup and runtime installers stay unwrapped. ``read_only`` files (the installer Nadare
        downloaded) are bound back so the program can run them; ``links=False`` skips the link
        relay for a program that opens no browser. An explicit Sandbox on means a missing
        bubblewrap or Flatpak is an error, not a fallback; on only by default, the program
        still starts and says why it is not sandboxed.
        """
        self._sandbox_inside = None
        if not self.secure_mode_enabled():
            self._sandbox_wrapped = False
            return argv, env
        if not env.get(gamesandbox.GAME_DRIVE_ENV):
            # Proton's drive S: as the game sees it without the Sandbox, asked on the host:
            # inside, umu would map the bound game folder itself (NTE: "root directory", #1614).
            drive = gamesandbox.game_drive(argv, env)
            if drive is not None:
                env = {**env, gamesandbox.GAME_DRIVE_ENV: drive}
        explicit = self._sandbox_insisted()
        thing = "this game" if subject == "game" else f"the {subject}"
        if sandbox.in_flatpak():
            if gamesandbox._flatpak_spawn() is None:
                if explicit:
                    raise BackendError(
                        "Sandbox needs flatpak-spawn, which Nadare could not find in this "
                        "Flatpak. Turn Sandbox off to play without it."
                    )
                self.info(
                    "Sandbox is on by default but needs flatpak-spawn, which Nadare could "
                    f"not find; {thing} runs without it.",
                    sandbox=False,
                )
                self._sandbox_wrapped = False
                return argv, env
            env_dir = os.environ.get("XDG_RUNTIME_DIR")
            if not env_dir or not Path(env_dir).is_dir():
                env_dir = tempfile.gettempdir()
            # The relay's pipe must sit outside XDG_RUNTIME_DIR, which Flatpak does not expose
            # to the game; Nadare's cache folder is in the app's own data area.
            pipe = None
            if links:
                cache = paths.cache_dir()
                try:
                    cache.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                pipe = self._start_link_relay(cache)
            try:
                wrapped = gamesandbox.wrap_flatpak(
                    argv,
                    env,
                    prefix=self._sandbox_prefix(),
                    install_dir=self._sandbox_game_dir(),
                    runner_dir=Path(env["PROTONPATH"]) if env.get("PROTONPATH") else None,
                    read_only=read_only,
                    env_dir=env_dir,
                    link_pipe=pipe,
                )
            except gamesandbox.BroadBindError as exc:
                if links:
                    self._stop_link_relay()
                # On by default or switched on: never run unsandboxed just because the game's
                # folder is too broad (it used to play unprotected with a log line; maintainer,
                # 2026-11-11). The user decides: move the game, or turn the Sandbox off for it.
                raise BackendError(_broad_message(exc)) from exc
            except BaseException:
                if links:
                    self._stop_link_relay()
                raise
            self.info(f"Sandbox: your home folder is hidden from {thing}.", sandbox=True)
            self._sandbox_wrapped = True
            return wrapped
        self._runtime_before_sandbox(argv, env)
        located = self._sandbox_check(subject)
        if located.binary is None:
            if not located.found:
                command = gamesandbox.install_hint(hostlibs.distro_family())
                self.info(
                    "Sandbox is on by default but needs bubblewrap, which Nadare could not "
                    f"find; {thing} runs without it. Install it with: {command}, or turn "
                    "Sandbox off to hide this note.",
                    sandbox=False,
                )
                self._sandbox_wrapped = False
                return argv, env
            if located.untrusted:
                self.info(
                    "Sandbox is on by default, but the game runtime was updated and Nadare has "
                    f"not been told to trust its new bubblewrap yet; {thing} runs without the "
                    'Sandbox. Restart Nadare and answer "Trust the new copy".',
                    sandbox=False,
                )
                self._sandbox_wrapped = False
                return argv, env
            self.info(
                f"Sandbox is on by default but cannot start here ({located.why}); {thing} "
                "runs without it. `nadare doctor` shows the fix.",
                sandbox=False,
            )
            self._sandbox_wrapped = False
            return argv, env
        pipe = self._start_link_relay() if links else None
        try:
            wrapped = gamesandbox.wrap(
                argv,
                env,
                prefix=self._sandbox_prefix(),
                install_dir=self._sandbox_game_dir(),
                runner_dir=Path(env["PROTONPATH"]) if env.get("PROTONPATH") else None,
                link_pipe=pipe,
                read_only=read_only,
                bwrap=located.binary,
                **self._sandbox_exemptions(),
            )
        except gamesandbox.BroadBindError as exc:
            if links:
                self._stop_link_relay()
            raise BackendError(_broad_message(exc)) from exc
        except BaseException:
            # Any other failure (a path loop, an OSError from a Path, ...) must not leave
            # the relay's thread, FIFO and temp dir behind for the life of the process.
            if links:
                self._stop_link_relay()
            raise
        self.info(f"Sandbox: your home folder is hidden from {thing}.", sandbox=True)
        self._sandbox_wrapped = True
        self._sandbox_inside = _inside_command(argv, wrapped)
        return wrapped

    def _run_unwrapped(self, argv: list[str], env: dict[str, str], *, on_line) -> int:  # noqa: ANN001
        """Run a setup step outside the Sandbox and return its exit code (backends supply it)."""
        raise NotImplementedError

    def _runtime_before_sandbox(self, argv: list[str], env: dict[str, str]) -> None:
        """Let umu set up its Steam Runtime before the Sandbox looks for the runtime's bubblewrap.

        A first launch through umu had no runtime yet, so the Sandbox found no copy to use
        (Ubuntu VM, 2026-11-14). ``umu-run ""`` downloads the runtime and creates the prefix
        and runs no game code; it runs outside the Sandbox like the rest of prefix setup.
        """
        if argv and Path(argv[0]).name == "mangohud":
            argv = argv[1:]  # CZN's optional overlay goes in front of umu-run (#1556)
            if argv[:1] == ["--dlsym"]:
                argv = argv[1:]
        marker = _umu_launcher_end(argv)
        if marker is None:
            return
        from . import umu

        if umu.runtime_downloaded():
            return
        from .events import mask

        self.info("Setting up the Steam Runtime and the prefix before the Sandbox starts.")
        try:
            # Line up to the umu program, then an empty game argument: setup only.
            code = self._run_unwrapped(
                [*argv[: marker + 1], ""],
                dict(env),
                on_line=lambda line: self.info(mask(line)),
            )
        except NotImplementedError:
            return
        if code != 0:
            self.info(f"umu's runtime setup ended with exit code {code}.")

    def _start_link_relay(self, directory: Path | None = None) -> Path | None:
        """Start the relay that lets the game open web links; its pipe, or ``None`` on failure.

        Links the game opens (a "Sign in with Google" button) reach the browser through Nadare,
        because the session bus the game would use is hidden too.
        """
        self._stop_link_relay()
        relay = linkrelay.LinkRelay(directory, on_open=self._note_link)
        self._link_relay = relay
        try:
            return relay.start()
        except OSError as exc:
            relay.stop()
            self._link_relay = None
            self.info(f"Sandbox: links in the game cannot open your browser ({exc}).")
            return None
        except BaseException:
            relay.stop()
            self._link_relay = None
            raise

    def _note_link(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).hostname or "a site"
        self.info(f"Sandbox: the game opened a link to {host} in your browser.")

    def _stop_link_relay(self) -> None:
        relay, self._link_relay = self._link_relay, None
        if relay is not None:
            relay.stop()


def _inside_command(
    argv: list[str], wrapped: tuple[list[str], dict[str, str]]
) -> tuple[list[str], dict[str, str]] | None:
    """``argv`` and the environment it sees inside bubblewrap's ``wrapped`` command.

    That is bubblewrap's environment plus the loader variables it passes with ``--setenv``.
    """
    outer, env = wrapped
    if len(outer) <= len(argv) or outer[-len(argv) - 1 :] != ["--", *argv]:
        return None
    inside = dict(env)
    flags = outer[: -len(argv) - 1]
    for i, part in enumerate(flags[:-2]):
        if part == "--setenv":
            inside[flags[i + 1]] = flags[i + 2]
    return list(argv), inside
