# SPDX-License-Identifier: GPL-3.0-or-later
"""Cheap, bounded evidence that a launch has produced a window, for the launch sheet.

Three signals, in order of reliability:

* The game's own output: DXVK and VKD3D-Proton print the swapchain/presenter they
  created at info level, and Nadare captures the launch's output line by line. This
  works on native Wayland where the X11 walk sees nothing, and it is the only signal
  that marks the first frame rather than start-up.
* X11 (and XWayland): a shown window of real size whose ``_NET_WM_PID`` belongs to the
  game's process tree. The window tree is walked with libX11 (:mod:`nadare.core.x11windows`),
  not ``_NET_CLIENT_LIST``: KWin under Wayland leaves that list empty, and using it made
  the check silently useless on the maintainer's own desktop. Wine's 1x1 IME/message
  helper windows are filtered out by size, so a launch cannot "finish" on one.
* Wayland, or a window whose owner is not known, and no presenter line: a graphics
  device opened or a D3D module mapped by a game process, but only after
  :data:`GRAPHICS_GRACE`. This is a last resort, because both happen during start-up:
  DXVK maps ``d3d11.dll`` and the game opens ``/dev/dri`` before its first frame
  (measured: window at 12–13 s on an NVIDIA PC), so a short grace would end the sheet
  while the screen is still black. ``libvulkan`` alone is only the loader and is never
  counted.

The RendererProbe is deliberately not involved: its fixed 20 s delay is not a window
signal, and its ``renderer:`` line is not a frame. Missing /proc entries or a missing X
display are absence of evidence, never a launch failure; the launch's exit and the
sheet's timeout still end the wait.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
import time
from pathlib import Path

from . import x11windows
from .events import Event

log = logging.getLogger(__name__)

#: How long the monitor looks for a window. The sheet's own hard timeout is the same.
WATCH_SECONDS = 90.0

#: One look per second is responsive enough for a launch sheet and cheap enough to leave
#: scanning ``/proc`` for a minute and a half.
POLL_INTERVAL = 1.0

#: How long a launch must take before a graphics device or D3D module counts as a window.
#: Both appear during start-up, before the first frame; see the module text.
GRAPHICS_GRACE = 20.0

#: A D3D module in a game process means it is setting up rendering. ``libvulkan`` alone
#: is only the loader, often mapped well before there is a window.
_D3D_MAPS_RE = re.compile(r"/(?:dxgi|d3d9|d3d10core|d3d11|d3d12|wined3d)\.(?:dll|so)(?:[.\s]|$)")

#: The optional prefix a Wine/DXVK/VKD3D log line can carry: up to four tokens, each a
#: hex thread id (``0124:``) or a decimal timestamp (``1234.567:``, Wine's
#: ``WINEDEBUG=+timestamp,+pid,+tid`` gives three). The first-frame detector and the run
#: log's info cap both use this one grammar, so a line cannot be dropped as noise
#: while being missed as a frame.
LOG_PREFIX = r"(?:[0-9A-Fa-f]+:|\d+\.\d+:){0,4}"

#: The line a translation layer prints once it has a swapchain to present to. DXVK names
#: the actual swapchain properties; VKD3D-Proton names its swapchain creation after its
#: function name (``info:d3d12_swapchain_create_vulkan_swapchain: …``). A DXVK/VKD3D
#: banner, a module name in a path, or a swapchain *error* is not a frame and must not match.
_FIRST_FRAME_RE = re.compile(
    r"^\s*" + LOG_PREFIX + r"info:"
    r"(?:[A-Za-z0-9_.-]+:)?\s*"
    r"(?:Presenter:\s+Actual swapchain properties"
    r"|(?:Creating|Reallocating) swapchain \(\d+\s*x\s*\d+\), BufferCount = \d+)"
)


def is_first_frame(line: str) -> bool:
    """True when ``line`` is the presenter/swapchain line of this launch's own output."""
    return bool(_FIRST_FRAME_RE.match(line))


def x11_windows() -> dict[int, int]:
    """Shown, real-size X11 windows as ``{window id: owning pid}``.

    Empty when X11 cannot be read (native Wayland, no libX11): that is not a failure,
    the graphics fallback and the timeout still apply.
    """
    try:
        windows = x11windows.list_windows_owned()
    except Exception:  # noqa: BLE001 - optional observation must never break Play
        return {}
    return dict(windows or ())


class WindowEvidence:
    """Decide whether this launch has produced a window yet.

    ``baseline`` holds the windows that existed before Play, so a launcher window that
    was already open cannot end a new launch on its own. ``game_processes`` says whether
    the game itself (not a launcher or installer helper) is running; a presenter line
    from a launcher that arrives first is ignored.
    """

    def __init__(
        self,
        baseline: set[int],
        *,
        proc_root: Path = Path("/proc"),
        game_processes=None,
    ):
        self.baseline = baseline
        self.proc_root = proc_root
        self._game_processes = game_processes
        self._presenter = threading.Event()
        self._stopped = threading.Event()

    @property
    def stopped(self) -> bool:
        """Whether the watch has ended: later output lines are no longer checked."""
        return self._stopped.is_set()

    def stop(self) -> None:
        """The watch has ended (window seen, gave up, or the launch ended): stop checking lines.

        Without this, every presenter line after a 90 s timeout still ran a process scan for a
        verdict nobody reads (#1852).
        """
        self._stopped.set()

    def feed(self, line: str) -> None:
        """Record one line of this launch's own output (its stdout/stderr).

        A presenter line only counts while the game process is alive: a publisher
        launcher or an installer helper can create a swapchain before the game exists.
        Once the frame is known, later lines are ignored before any process scan: DXVK
        reallocates its swapchain on every resize, and a full ``/proc`` walk per line
        would stall the thread that drains the game's output (#1821 follow-up).
        """
        if self._presenter.is_set() or self._stopped.is_set():
            return
        if not is_first_frame(line):
            return
        if self._game_processes is not None:
            try:
                if not self._game_processes():
                    return
            except Exception:  # noqa: BLE001 - optional observation must never break Play
                return
        self._presenter.set()

    def _game_tree(self, pids) -> set[int]:
        """The launch's pids plus their descendants (a launcher opens the game's window)."""
        owned = set(pids)
        parents: dict[int, int] = {}
        try:
            for entry in self.proc_root.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                    parents[int(entry.name)] = int(stat[1])
                except (OSError, ValueError, IndexError):
                    continue
        except OSError:
            return owned  # No process tree to expand; the toplevel check still works.
        while True:
            children = {pid for pid, parent in parents.items() if parent in owned}
            if children <= owned:
                break
            owned.update(children)
        return owned

    def _namespace_pids(self, owned: set[int]) -> set[int]:
        """The pids the game's processes see inside their own PID namespace.

        The Sandbox gives a game its own PID namespace (#1593), and Wine writes the pid
        it sees (the last ``NSpid`` field) into ``_NET_WM_PID``, not the host pid.
        """
        inner: set[int] = set()
        for pid in owned:
            try:
                status = (self.proc_root / str(pid) / "status").read_text()
            except OSError:
                continue
            for line in status.splitlines():
                if line.startswith("NSpid:"):
                    fields = line.split()[1:]
                    if len(fields) > 1 and fields[-1].isdigit():
                        inner.add(int(fields[-1]))
                    break
        return inner

    def _owned_pids(self, pids) -> tuple[set[int], set[int]]:
        """The game's pids, their descendants, and the pids they see in their namespace."""
        owned = self._game_tree(pids)
        return owned, self._namespace_pids(owned)

    def owned_windows(self, pids, windows: dict[int, int]) -> dict[int, int]:
        """Windows with a process-tree PID, excluding unqualified namespace-local PIDs.

        The launch sheet may use namespace PIDs as observation evidence. Stop must
        not: separate games can report the same local PID to the shared X server.
        Without qualified client ownership, those windows use the signal fallback.
        """
        owned, inner = self._owned_pids(pids)
        return {
            window: pid
            for window, pid in windows.items()
            if pid in owned and pid not in inner
        }

    def owned_by_client(self, pids, clients: dict[int, int]) -> dict[int, int]:
        """Windows whose X client, as the X server names it, is in the game's process tree.

        ``clients`` comes from :func:`x11windows.client_pids`: host pids, unique, so no
        namespace-local pid has to be excluded here (unlike :meth:`owned_windows`).
        """
        owned, _inner = self._owned_pids(pids)
        return {window: pid for window, pid in clients.items() if pid in owned}

    def seen(self, pids, elapsed: float, windows: dict[int, int]) -> bool:
        # The launch's own presenter line is the first frame: it needs no process rule.
        if self._presenter.is_set():
            return True
        if pids is None:
            return False  # no process rule: only the presenter line can end the wait
        owned, inner = self._owned_pids(pids)
        new = {window: pid for window, pid in windows.items() if window not in self.baseline}
        if any(pid in owned or pid in inner for pid in new.values()):
            return True
        if elapsed < GRAPHICS_GRACE:
            return False
        for pid in owned:
            entry = self.proc_root / str(pid)
            try:
                for fd in (entry / "fd").iterdir():
                    try:
                        target = os.readlink(fd)
                    except OSError:
                        continue
                    if target.startswith(("/dev/dri/", "/dev/nvidia")):
                        return True
            except OSError:
                pass
            try:
                maps = (entry / "maps").read_text(errors="replace").lower()
            except OSError:
                continue
            if _D3D_MAPS_RE.search(maps):
                return True
        return False


@contextlib.contextmanager
def watch(game_pids, emit, cancel, *, game_processes=None):
    """Observe the existing launch, with the same process ownership used by Stop.

    ``game_pids`` returns the live pids Stop would treat as the game's (the backend layer
    builds it, see ``backends.wine.game_pids``); ``None`` means there is no reliable
    process rule, and only the launch's own presenter line can end the wait.
    ``game_processes`` says whether the game itself (not a launcher or helper) is
    running; presenter lines from a launcher are then ignored. Yields the
    :class:`WindowEvidence` around ``launchgate.play`` so a window made during the checks
    is still a change from ``baseline``, and the caller can ``feed`` the launch's output
    lines to it.
    """
    # Before Play, so a window this launch opens is always new. Captured on the
    # caller's thread: the monitor must not race the game's first window.
    evidence = WindowEvidence(
        set(x11_windows()) if game_pids is not None else set(),
        game_processes=game_processes,
    )
    done = threading.Event()

    def monitor() -> None:
        started = time.monotonic()
        logged: set[str] = set()

        def failed(what: str) -> None:
            # One warning per kind of read: a later, different failure still shows.
            if what not in logged:
                logged.add(what)
                log.warning("launch window %s failed; still watching", what, exc_info=True)

        try:
            while not done.wait(POLL_INTERVAL) and not cancel.cancelled:
                elapsed = time.monotonic() - started
                if elapsed >= WATCH_SECONDS:
                    return
                try:
                    pids = None if game_pids is None else game_pids()
                except Exception:  # noqa: BLE001 - one bad /proc read is not the launch
                    # A transient process read must not end the watch: log it once and
                    # keep polling, so the presenter line still ends the sheet (SIM F3).
                    failed("process read")
                    pids = None  # no process rule this poll; the presenter still counts
                try:
                    windows = x11_windows() if pids is not None else {}
                except Exception:  # noqa: BLE001 - the same rule for the window list
                    failed("window list")
                    windows = {}
                try:
                    seen = evidence.seen(pids, elapsed, windows)
                except Exception:  # noqa: BLE001 - the same rule for any other read
                    failed("check")
                    continue
                if seen:
                    # The launch may end while this scan was running; a late event would
                    # land on whatever the window shows next.
                    if done.is_set() or cancel.cancelled:
                        return
                    emit(Event("window_seen"))
                    return
        except Exception:  # noqa: BLE001 - optional observation must never break Play
            return
        finally:
            evidence.stop()

    thread = threading.Thread(target=monitor, name="launch-window", daemon=True)
    thread.start()
    try:
        yield evidence
    finally:
        done.set()
        evidence.stop()
        thread.join(timeout=0.5)
