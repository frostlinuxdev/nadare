# SPDX-License-Identifier: GPL-3.0-or-later
"""1,000-world seeded simulation of one merged change: the launch sheet (#1821, b28 audit).

The sheet ends when the game's first frame is seen (:mod:`nadare.core.launch_window`)
or when the launch ends; the run log keeps a bounded trace of DXVK/VKD3D output
(:class:`nadare.gui.workers.BackendWorker`). Both decisions are event-driven and
timing-sensitive, so this module replays random worlds against the shipped code:

* a virtual clock and one explicit poll handshake per second of world time, so a
  90 s watch costs no real time and every ordering is deterministic;
* a fake ``/proc`` tree and a fake X11 window table built in ``tmp_path``;
* a random stream of log lines: every known presenter format, near misses, binary
  junk, huge lines, and Nadare's own ``runner:``/``exec:``/``env:`` lines;
* random process states (launcher only, WebView2 helper, game up, game gone, no
  process rule) with random timings, Stop and Hide.

Each world is seeded from :class:`random.Random`. The long run is off unless
``NADARE_SIM=1``; ``SIM_SEEDS`` lowers the count and ``SIM_SEED`` replays one world.
A 20-world smoke test always runs. A failing world names its seed, prints its recipe
and the one command that replays it.

What this model does **not** cover (see the report for the reasoning): real X11
clients (launcher windows are modelled as windowless before the game), ProcessScanRound,
the renderer probe, and text decoded from the child with ``errors="replace"`` (so no
lone surrogates ever reach the log).
"""

from __future__ import annotations

import contextlib
import math
import os
import random
import re
import shutil
import threading
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt

from nadare.core import launch_window, runlog
from nadare.core.config import Config
from nadare.core.events import Cancel, Event
from nadare.gui import workers

WATCH_SECONDS = launch_window.WATCH_SECONDS
GRAPHICS_GRACE = launch_window.GRAPHICS_GRACE

#: Real DXVK / VKD3D-Proton first-frame lines. Every one is a window signal today.
PRESENTER_LINES = (
    "info:  Presenter: Actual swapchain properties:",
    "info:  Presenter: Actual swapchain properties: 1920x1080",
    "info:  Presenter: Actual swapchain properties",
    "0124:info:  Presenter: Actual swapchain properties:",
    "0008:0008:info:  Presenter: Actual swapchain properties:",
    "  info:  Presenter: Actual swapchain properties:",
    "info:  Creating swapchain (1280 x 720), BufferCount = 3.",
    "info: Creating swapchain (800x600), BufferCount = 2",
    "info:  Reallocating swapchain (1920 x 1080), BufferCount = 2.",
    "0124:info:d3d12_swapchain_create_vulkan_swapchain: "
    "Creating swapchain (1280 x 720), BufferCount = 3.",
    "info:vkd3d-proton: Reallocating swapchain (1920 x 1080), BufferCount = 2.",
    "0124:info:vkd3d-proton: Creating swapchain (1024x768), BufferCount = 2.",
)

#: Lines that look like a first frame but are not one: banner, error, wrong level,
#: crunched spacing, junk in front, and a swapchain line without its counts.
NEAR_MISS_LINES = (
    "info:  DXVK: v3.1.1-19-g601930949",
    "info:  Presenter: Failed to create Vulkan swapchain:",
    "info:  Presenter: Got -1000208000, recreating swapchain",
    "info:  Created cache file: C:\\users\\steamuser\\AppData\\Local\\dxvk\\x.dxvk.bin",
    "renderer: DXVK on Vulkan",
    "INFO:  Presenter: Actual swapchain properties:",
    "info   Presenter: Actual swapchain properties:",
    "info:  Creating swapchain (1280 x 720)",
    "info:  Reallocating swapchain (1024x768), BufferCount=2.",
    "0124:err:d3d12_swapchain_create_vulkan_swapchain: "
    "Creating swapchain (1280 x 720), BufferCount = 3.",
    "0124:info:d3d12_swapchain_create_vulkan_swapchain: Failed to create swapchain",
    "err:   DXGI: Failed to parse display metadata",
    "warn:  Presenter: Actual swapchain properties:",
    "fixme: Presenter: Actual swapchain properties:",
    "\x00info:  Presenter: Actual swapchain properties:",
)

#: Wine's ``WINEDEBUG=+timestamp`` prefix (decimal) with a presenter line. The cap and
#: the detector share one grammar now, so these are first frames too (#1821 follow-up).
DECIMAL_STAMP_LINES = (
    "1234.567:info:  Presenter: Actual swapchain properties:",
    "0123.456:0124:info: Creating swapchain (1280 x 720), BufferCount = 3.",
)

#: DXVK/VKD3D ``info:`` lines that are not a frame; they must still be capped.
LAYER_NOISE_LINES = (
    "info:  DXVK: v3.1.1",
    "info:  Found device: NVIDIA GeForce RTX 4070",
    "info:  Creating cache file: /home/x/.cache/dxvk/x.dxvk-cache",
    "info:  Presenter: Got -1000208000, recreating swapchain",
    "info:  Presenter: Failed to create Vulkan swapchain:",
    "info:d3d12_swapchain_create_vulkan_swapchain: Failed to create swapchain",
    "info:  D3D11Device: Creating device",
    "0008:info:  DXVK: v3.1.1",
    "info:vkd3d-proton: Creating swapchain (1280 x 720)",
)

#: The log cap's own rule, restated here on purpose: a genuine ``info:`` line starts
#: with the shared prefix (hex thread id and/or decimal timestamp) and ``info:``.
#: Keeping this separate from ``workers._LAYER_INFO_RE`` means a narrowed cap regex
#: shows up as too many kept lines.
_INFO_PREFIX = re.compile(r"^\s*(?:(?:[0-9A-Fa-f]+|\d+\.\d+):)*info:")

OWN_LINES = (
    "runner: GE-Proton11-7",
    "exec: umu-run game.exe --launch",
    "env: DXVK_LOG_LEVEL=info",
    "runner: Proton Experimental",
    "exec: /usr/bin/wine C:\\games\\game.exe",
    "env: WINEPREFIX=/home/x/.local/share/nadare/prefix",
)

PROBLEM_LINES = (
    "err:   DXGI: Failed to parse display metadata",
    "warn:  D3D11: Unsupported format",
    "0124:err:d3d12_swapchain_create_vulkan_swapchain: Failed to create swapchain",
    "0124:warn:vkd3d-proton: Swapchain is out of date",
    "err:   DXVK: Device lost",
)


# ---------------------------------------------------------------------------
# the small shared machinery
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _patched(module, **values):
    """Patch module attributes for one world and put them back afterwards."""
    old = {name: getattr(module, name) for name in values}
    for name, value in values.items():
        setattr(module, name, value)
    try:
        yield
    finally:
        for name, value in old.items():
            setattr(module, name, value)


@dataclass(frozen=True)
class _Action:
    at: float
    kind: str
    payload: object = None


_ORDER = {"state": 0, "feed": 1, "hide": 2, "stop": 3}


@dataclass(frozen=True)
class _Emission:
    at: float
    kind: str
    state: str
    windows: bool
    graphics: bool


class WorldFailed(AssertionError):
    """One world failed one invariant: seed, recipe, and the command that replays it."""

    def __init__(self, seed: int, part: str, invariant: str, detail: str, recipe: str) -> None:
        self.seed = seed
        self.part = part
        self.invariant = invariant
        self.detail = detail
        self.recipe = recipe
        super().__init__(
            f"seed={seed} part={part} invariant={invariant}\n"
            f"  recipe: {recipe}\n"
            f"  detail: {detail}\n"
            f"  repro:  NADARE_SIM=1 SIM_SEED={seed} .venv/bin/python -m pytest "
            "tests/sim/test_sim_launch.py -q -k one_thousand"
        )


# ---------------------------------------------------------------------------
# the virtual clock: one poll handshake per tick, no real waiting
# ---------------------------------------------------------------------------
class _Bus:
    """The monitor's ``done.wait(POLL_INTERVAL)``, made deterministic.

    The monitor parks here at the top of every loop. The driver grants one tick; the
    bus advances the virtual clock, applies every action due at that time, takes a state
    snapshot, and only then lets the monitor run its body (``pids``, ``x11_windows``,
    ``seen``). No ordering depends on real thread scheduling.
    """

    def __init__(self, step: float, script: list[_Action], apply, snapshot) -> None:
        self.cond = threading.Condition()
        self.step = step
        self.t = 0.0
        self.script = sorted(script, key=lambda a: (a.at, _ORDER[a.kind]))
        self.next = 0
        self.apply = apply
        self.snapshot = snapshot
        self.at_tick = False
        self.go = False
        self.stop = False
        self.monitor_done = False
        self.ticks = 0
        self.limit = math.ceil(WATCH_SECONDS / step) + 2

    # -- monitor side ----------------------------------------------------
    def pause(self, event: _BusEvent) -> bool:
        with self.cond:
            if event.is_set() or self.stop:
                self.monitor_done = True
                self.cond.notify_all()
                return True
            self.at_tick = True
            self.cond.notify_all()
            while not self.go and not event.is_set() and not self.stop:
                self.cond.wait()
            self.go = False
            if event.is_set() or self.stop:
                self.at_tick = False
                self.monitor_done = True
                self.cond.notify_all()
                return True
            self.t = round(self.t + self.step, 9)
            self.ticks += 1
            while self.next < len(self.script) and self.script[self.next].at <= self.t:
                action = self.script[self.next]
                self.next += 1
                self.apply(action)
            self.snapshot()
            self.at_tick = False
            self.cond.notify_all()
            return False

    # -- driver side -----------------------------------------------------
    def _await(self, predicate, what: str) -> None:
        deadline = time.monotonic() + 5.0
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"simulator deadlock waiting for {what}")
            self.cond.wait(min(0.05, remaining))

    def drive(self, end_tick: int) -> None:
        end_tick = max(1, min(end_tick, self.limit))
        while True:
            with self.cond:
                self._await(lambda: self.at_tick or self.monitor_done, "a poll")
                if self.monitor_done or not self.at_tick:
                    return
                if self.ticks >= end_tick:
                    self.stop = True
                    self.cond.notify_all()
                    return
                granted = self.ticks
                self.go = True
                self.cond.notify_all()
                # ``ticks`` changes inside the monitor's tick, so a monitor that has
                # already asked for its next tick is not mistaken for a stuck one.
                self._await(
                    lambda granted=granted: self.ticks != granted or self.monitor_done,
                    "the poll body",
                )


class _BusEvent:
    """``threading.Event`` for the monitor's ``done``, released through the bus."""

    def __init__(self, bus: _Bus) -> None:
        self._set = False
        self._bus = bus

    def set(self) -> None:
        with self._bus.cond:
            self._set = True
            self._bus.cond.notify_all()

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float) -> bool:  # noqa: ARG002 - the bus owns the clock
        return self._bus.pause(self)


class _FakeThreads:
    """``launch_window.threading``: a real thread, with its exit announced to the bus."""

    def __init__(self, bus: _Bus) -> None:
        self._bus = bus

    def Event(self) -> _BusEvent:  # noqa: N802 - mirrors threading.Event
        return _BusEvent(self._bus)

    def Thread(self, target, name=None, daemon=None):  # noqa: N802 - threading.Thread
        def run() -> None:
            try:
                target()
            finally:
                with self._bus.cond:
                    self._bus.monitor_done = True
                    self._bus.cond.notify_all()

        return threading.Thread(target=run, name=name, daemon=daemon)


# ---------------------------------------------------------------------------
# a watch world: fake /proc, fake X11, random states and lines
# ---------------------------------------------------------------------------
class _WatchWorld:
    TOP_PID = 4241
    GAME_PID = 4242
    WINDOW = 9000

    def __init__(self, seed: int, rng: random.Random, root: Path) -> None:
        self.seed = seed
        self.rng = rng
        self.proc = root / "proc"
        self.proc.mkdir(parents=True)
        self.step = rng.choice((0.25, 0.5, 1.0, 1.7, 2.5))
        self.rule = rng.choices(("explicit", "fallback", "none"), (0.7, 0.2, 0.1))[0]
        self.graphics = rng.choices(("d3d", "dri", "both", "none"), (0.5, 0.15, 0.25, 0.1))[0]
        self.x11 = rng.random() < 0.2
        self.tree = rng.random() < 0.3
        self.start_state = rng.choices(
            ("launcher", "helper", "game", "gone"), (0.45, 0.25, 0.2, 0.1)
        )[0]
        self.state = "gone"
        self.pids: list[int] = []
        self.game = False
        self.windows: dict[int, int] = {}
        self.cancel_token = Cancel()
        self.emits: list[_Emission] = []
        self.snaps: list[tuple] = []
        self.applied: list[tuple[float, _Action, bool]] = []
        self.hides: list[float] = []
        self.stop_tick: float | None = None
        self.evidence = None
        self.degraded = False
        self.seen_error: str | None = None
        self.faults = Counter()
        self.stats = Counter()
        self.pids_raise: tuple[float, float] | None = None
        self.gate_raise: tuple[float, float] | None = None
        self._raised = False
        self.script: list[_Action] = []
        self.end_time = 0.0
        self.bus = _Bus(self.step, self.script, self._apply, self._snapshot)
        self._build()

    # -- the timeline ----------------------------------------------------
    def _build(self) -> None:
        rng = self.rng
        acts: list[_Action] = [_Action(0.0, "state", self.start_state)]
        states = [(0.0, self.start_state)]
        game_entry = 0.0 if self.start_state == "game" else None
        game_exit = None
        if game_entry is None and rng.random() < 0.8:
            game_entry = round(rng.uniform(1.5, 35.0), 3)
            acts.append(_Action(game_entry, "state", "game"))
            states.append((game_entry, "game"))
        if game_entry is not None and rng.random() < 0.7:
            game_exit = round(game_entry + rng.uniform(1.0, 40.0), 3)
            acts.append(_Action(game_exit, "state", "gone"))
            states.append((game_exit, "gone"))
        elif game_entry is None and rng.random() < 0.6:
            last = states[-1][0]
            exit_at = round(last + rng.uniform(1.0, 40.0), 3)
            acts.append(_Action(exit_at, "state", "gone"))
            states.append((exit_at, "gone"))

        def state_at(when: float) -> str:
            found = states[0][1]
            for at, name in states:
                if at <= when:
                    found = name
            return found

        # A presenter line once the game runs: the invariant I3 must see it fire.
        if game_entry is not None:
            lines = rng.sample(PRESENTER_LINES, rng.randint(1, 2))
            for index, line in enumerate(lines):
                when = game_entry + rng.uniform(0.4, 4.0) + index * rng.uniform(0.5, 2.0)
                if game_exit is not None and when >= game_exit:
                    when = game_entry + 0.4
                acts.append(_Action(round(when, 3), "feed", _Line(line, "presenter")))
                if rng.random() < 0.25:
                    acts.append(_Action(round(when, 3), "feed", _Line(line, "presenter")))
                    self.faults["presenter_duplicate"] += 1
        # A presenter line before the game (or after it): the gate must ignore it.
        if rng.random() < 0.45:
            pres = _Line(rng.choice(PRESENTER_LINES), "presenter")
            if game_entry is not None and rng.random() < 0.5:
                when = round(max(0.3, game_entry - rng.uniform(0.3, 3.0)), 3)
            else:
                when = round(rng.uniform(0.3, WATCH_SECONDS - 5), 3)
            acts.append(_Action(when, "feed", pres))
            self.faults["presenter_outside_game"] += 1
        # Noise, near misses and junk around them.
        for _ in range(rng.randint(0, 3)):
            acts.append(
                _Action(
                    round(rng.uniform(0.2, WATCH_SECONDS - 2), 3),
                    "feed",
                    _Line(rng.choice(LAYER_NOISE_LINES), "layer"),
                )
            )
        for _ in range(rng.randint(0, 4)):
            acts.append(
                _Action(
                    round(rng.uniform(0.2, WATCH_SECONDS - 2), 3),
                    "feed",
                    _Line(rng.choice(NEAR_MISS_LINES), "near"),
                )
            )
        if rng.random() < 0.3:
            for _ in range(rng.randint(1, 3)):
                acts.append(
                    _Action(
                        round(rng.uniform(0.2, WATCH_SECONDS - 2), 3),
                        "feed",
                        _Line(rng.choice(DECIMAL_STAMP_LINES), "presenter"),
                    )
                )
                self.faults["decimal_stamp_line"] += 1
        if rng.random() < 0.25:
            acts.append(
                _Action(
                    round(rng.uniform(0.2, WATCH_SECONDS - 2), 3),
                    "feed",
                    _Line("\x00\x01zq-sim-junk\x00info:  Presenter: x", "junk"),
                )
            )
            self.faults["binary_junk"] += 1
        if rng.random() < 0.2:
            acts.append(
                _Action(
                    round(rng.uniform(0.2, WATCH_SECONDS - 2), 3),
                    "feed",
                    _Line("H" * rng.randint(4000, 20000) + " info:  Presenter: x", "junk"),
                )
            )
            self.faults["huge_line"] += 1
        # Stop and Hide.
        if rng.random() < 0.2:
            stop_at = round(rng.uniform(1.0, 60.0), 3)
            acts.append(_Action(stop_at, "stop"))
            self.faults["stop"] += 1
        if rng.random() < 0.3:
            acts.append(_Action(round(rng.uniform(1.0, 30.0), 3), "hide"))
            self.faults["hide"] += 1
        # A rule that fails once: safety must hold, the first frame may be missed.
        if rng.random() < 0.04:
            at = round(rng.uniform(1.0, 30.0), 3)
            self.pids_raise = (at, at + 2.0)
            self.faults["game_pids_error"] += 1
        if self.rule != "none" and rng.random() < 0.04:
            at = round(rng.uniform(1.0, 30.0), 3)
            self.gate_raise = (at, at + 2.0)
            self.faults["game_rule_error"] += 1

        self.script = sorted(acts, key=lambda a: (a.at, _ORDER[a.kind]))
        stop_at = next((a.at for a in self.script if a.kind == "stop"), None)
        gone_at = next(
            (a.at for a in self.script if a.kind == "state" and a.payload == "gone"), None
        )
        ends = [WATCH_SECONDS + 2 * self.step]
        if stop_at is not None:
            ends.append(stop_at)
        if gone_at is not None:
            ends.append(gone_at)
        self.end_time = min(ends)
        self.bus = _Bus(self.step, self.script, self._apply, self._snapshot)

    # -- the world's state ----------------------------------------------
    def _snapshot(self) -> None:
        self.snaps.append(
            (
                self.bus.t,
                self.state,
                tuple(self.pids),
                self.game,
                bool(self.windows),
                self.state == "game" and self.rule != "none" and self.graphics != "none",
            )
        )

    def _apply(self, action: _Action) -> None:
        if action.kind == "state":
            self._set_state(action.payload)
        elif action.kind == "feed":
            line = action.payload
            accepted = True
            if self.rule != "none":
                try:
                    accepted = bool(self.gate())
                except Exception:  # noqa: BLE001 - the product ignores a broken rule too
                    accepted = False
                    self.degraded = True
            self.applied.append((self.bus.t, action, accepted))
            try:
                self.evidence.feed(line.text)
            except Exception:  # noqa: BLE001 - fail the world, not the simulator thread
                raise AssertionError(f"feed raised on {line.text!r}") from None
        elif action.kind == "hide":
            self.hides.append(self.bus.t)
        elif action.kind == "stop":
            self.stop_tick = self.bus.t
            self.cancel_token.cancel()

    def _set_state(self, state: str) -> None:
        self.state = state
        self.game = state == "game"
        if self.rule == "none" or state == "gone":
            self.pids = []
        elif state == "game":
            self.pids = [self.GAME_PID] if not self.tree else [self.TOP_PID, self.GAME_PID]
        else:
            self.pids = [self.TOP_PID]
        self.windows = {self.WINDOW: self.GAME_PID} if (self.x11 and state == "game") else {}
        self._sync_dirs()

    def _sync_dirs(self) -> None:
        alive: dict[int, int] = {}
        if self.rule != "none" and self.state != "gone":
            alive[self.TOP_PID] = 1
        if self.state == "game" and self.rule != "none":
            alive[self.GAME_PID] = self.TOP_PID if self.tree else 1
        for child in list(self.proc.iterdir()):
            if child.name.isdigit() and int(child.name) not in alive:
                shutil.rmtree(child)
        for pid, parent in alive.items():
            self._write_pid(pid, parent)

    def _write_pid(self, pid: int, parent: int) -> None:
        entry = self.proc / str(pid)
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "stat").write_text(f"{pid} (game.exe) S {parent} 0 0\n", encoding="utf-8")
        (entry / "status").write_text(f"Name:\tgame.exe\nNSpid:\t{pid}\n", encoding="utf-8")
        if pid != self.GAME_PID:
            return
        fd = entry / "fd"
        fd.mkdir(exist_ok=True)
        if self.graphics in ("dri", "both"):
            link = fd / "3"
            if not link.is_symlink():
                os.symlink("/dev/dri/renderD128", link)
        if self.graphics in ("d3d", "both"):
            (entry / "maps").write_text(
                "7f00-7f01 r-xp 00000000 00:00 0 /wine/lib/wine/x86_64-unix/d3d11.dll\n",
                encoding="utf-8",
            )

    # -- the product's callables ----------------------------------------
    def game_pids(self):
        if self.pids_raise is not None and not self._raised:
            low, high = self.pids_raise
            if low <= self.bus.t <= high:
                self._raised = True
                self.degraded = True
                raise OSError("simulated /proc failure")
        return list(self.pids)

    def gate(self) -> bool:
        if self.gate_raise is not None:
            low, high = self.gate_raise
            if low <= self.bus.t <= high:
                self.degraded = True
                raise OSError("simulated rule failure")
        if self.rule == "explicit":
            return self.game
        return bool(self.pids)

    def x11_windows(self) -> dict[int, int]:
        return dict(self.windows)

    def emit(self, event: Event) -> None:
        self.emits.append(
            _Emission(
                self.bus.t,
                event.kind,
                self.state,
                bool(self.windows),
                self.state == "game" and self.graphics != "none" and self.rule != "none",
            )
        )

    # -- run and check ---------------------------------------------------
    @property
    def end_tick(self) -> int:
        return max(1, math.ceil(self.end_time / self.step))

    def run(self) -> None:
        real_evidence = launch_window.WindowEvidence
        proc = self.proc

        def evidence(baseline, **kwargs):
            probe = real_evidence(baseline, proc_root=proc, **kwargs)
            real_seen = probe.seen

            def seen(*args, **more):
                try:
                    return real_seen(*args, **more)
                except BaseException as exc:  # noqa: BLE001 - recorded, then swallowed
                    self.seen_error = f"{type(exc).__name__}: {exc}"
                    raise

            probe.seen = seen  # type: ignore[method-assign]
            return probe

        clock = SimpleNamespace(monotonic=lambda: self.bus.t)
        with _patched(
            launch_window,
            time=clock,
            threading=_FakeThreads(self.bus),
            x11_windows=self.x11_windows,
            WindowEvidence=evidence,
        ):
            with launch_window.watch(
                None if self.rule == "none" else self.game_pids,
                self.emit,
                self.cancel_token,
                game_processes=None if self.rule == "none" else self.gate,
            ) as watched:
                self.evidence = watched
                self.bus.drive(self.end_tick)
        self._check()
        self.stats[f"rule_{self.rule}"] += 1
        self.stats["watch_worlds"] += 1
        self.stats["polls"] += self.bus.ticks
        self.stats["emitted" if self.emits else "no_emission"] += 1
        if any(snap[5] for snap in self.snaps):
            self.stats["graphics_evidence"] += 1
        if self.x11:
            self.stats["x11_window"] += 1
        if self.hides:
            self.stats["hid"] += 1
        if self.degraded:
            self.stats["degraded"] += 1

    def _failed(self, invariant: str, detail: str) -> WorldFailed:
        timeline = " ".join(f"{a.at:g}{a.kind[0]}" for a in self.script[:14])
        recipe = (
            f"step={self.step:g} rule={self.rule} graphics={self.graphics} "
            f"x11={self.x11} tree={self.tree} start={self.start_state} "
            f"end={self.end_time:g} ticks={self.bus.ticks} timeline=[{timeline}]"
        )
        return WorldFailed(self.seed, "watch", invariant, detail, recipe)

    def _check(self) -> None:
        if self.seen_error:
            # ``watch`` swallows monitor exceptions; a world must still fail on one.
            raise self._failed("no-exception", f"WindowEvidence.seen raised: {self.seen_error}")
        emits = self.emits
        first = emits[0].at if emits else None
        if len(emits) > 1:
            raise self._failed("window_seen-at-most-once", f"{len(emits)} events")
        for emission in emits:
            if emission.kind != "window_seen":
                raise self._failed("only-window_seen", f"got {emission.kind!r}")
        # window_seen never before the game process exists, when there is a game rule.
        # The presenter line is gated at the moment it arrives, so a signal that arrived
        # while the game ran may legitimately be polled after the game has gone.
        from_presenter = first is not None and any(
            at <= first
            and action.kind == "feed"
            and action.payload.kind == "presenter"
            and accepted
            for at, action, accepted in self.applied
        )
        if from_presenter:
            self.stats["presenter_fired"] += 1
        if emits and self.rule == "explicit":
            emission = emits[0]
            if not (from_presenter or emission.windows):
                if not emission.graphics:
                    raise self._failed(
                        "no-window-before-the-game",
                        f"window_seen at t={emission.at:g} without an accepted presenter "
                        f"line, an owned window or the graphics fallback "
                        f"(state={emission.state})",
                    )
                if emission.at < GRAPHICS_GRACE:
                    raise self._failed(
                        "20s-fallback-grace",
                        f"the graphics fallback ended the sheet at t={emission.at:g}, "
                        f"before its {GRAPHICS_GRACE:g} s grace",
                    )
        # A valid presenter line while the game runs must end the watch on that poll.
        for at, action, accepted in self.applied:
            if action.kind != "feed" or action.payload.kind != "presenter" or not accepted:
                continue
            if at >= WATCH_SECONDS:
                continue
            if self.stop_tick is not None and at >= self.stop_tick:
                self.faults["stop_race"] += 1
                continue
            if first is None or first > at:
                raise self._failed(
                    "presenter-line-fires",
                    f"presenter {action.payload.text[:60]!r} at t={at:g} did not fire "
                    f"(emits={[e.at for e in emits]}, state={self.state})",
                )
        # The graphics fallback ends the sheet at GRAPHICS_GRACE, never later. A Stop
        # applied on a poll skips that poll's scan, so only earlier polls count.
        if not self.degraded:
            stop_tick = self.stop_tick
            deadline = next(
                (
                    snap[0]
                    for snap in self.snaps
                    if snap[1] == "game"
                    and snap[5]
                    and GRAPHICS_GRACE <= snap[0] < WATCH_SECONDS
                    and (stop_tick is None or snap[0] < stop_tick)
                ),
                None,
            )
            if deadline is not None and (first is None or first > deadline):
                raise self._failed(
                    "20s-fallback",
                    f"graphics evidence since before t={deadline:g} but emits="
                    f"{[e.at for e in emits]} (grace={GRAPHICS_GRACE:g})",
                )
            if deadline is not None:
                self.stats["fallback_deadline"] += 1
                if (
                    first is not None
                    and first >= GRAPHICS_GRACE
                    and not from_presenter
                    and not emits[0].windows
                ):
                    self.stats["fallback_fired"] += 1
        if not self.bus.monitor_done:
            raise self._failed("watch-ends", f"monitor alive after {self.bus.ticks} polls")
        if self.bus.ticks > self.bus.limit:
            raise self._failed("watch-ends", f"{self.bus.ticks} polls > {self.bus.limit}")
        leftovers = [
            thread.name
            for thread in threading.enumerate()
            if thread.name == "launch-window" and thread.is_alive()
        ]
        if leftovers:
            raise self._failed("no-thread-left", f"threads still alive: {leftovers}")


@dataclass(frozen=True)
class _Line:
    text: str
    kind: str


# ---------------------------------------------------------------------------
# a log world: the worker's cap on DXVK/VKD3D info lines
# ---------------------------------------------------------------------------
def _log_stream(seed: int, rng: random.Random, faults: Counter) -> list[_Line]:
    lines: list[_Line] = []
    index = 0

    def make(text: str, kind: str) -> _Line:
        nonlocal index
        index += 1
        # Fixed width and under 12 chars (``events.mask`` hides generated-looking longer
        # runs), so the marker survives the log and no marker is part of another one.
        return _Line(f"{text} zq{index:04d}", kind)

    def add(text: str, kind: str) -> None:
        lines.append(make(text, kind))

    for _ in range(rng.randint(60, 160)):
        pick = rng.random()
        if pick < 0.34:
            add(rng.choice(LAYER_NOISE_LINES), "layer")
        elif pick < 0.44:
            add(rng.choice(PRESENTER_LINES), "presenter")
        elif pick < 0.56:
            add(rng.choice(NEAR_MISS_LINES), "near")
        elif pick < 0.70:
            add(rng.choice(PROBLEM_LINES), "problem")
        elif pick < 0.82:
            add(rng.choice(OWN_LINES), "own")
        else:
            add(rng.choice(("plain game chatter", "\x1b[1;31mcolour junk", "tab\tsep")), "junk")
    # Guarantee every invariant has something to lose.
    for _ in range(max(0, 30 - sum(1 for line in lines if line.kind == "layer"))):
        add(rng.choice(LAYER_NOISE_LINES), "layer")
    for _ in range(max(0, 3 - sum(1 for line in lines if line.kind == "presenter"))):
        add(rng.choice(PRESENTER_LINES), "presenter")
    for _ in range(max(0, 3 - sum(1 for line in lines if line.kind == "problem"))):
        add(rng.choice(PROBLEM_LINES), "problem")
    for _ in range(max(0, 3 - sum(1 for line in lines if line.kind == "own"))):
        add(rng.choice(OWN_LINES), "own")
    rng.shuffle(lines)
    if rng.random() < 0.5:
        burst = [make(rng.choice(LAYER_NOISE_LINES), "layer") for _ in range(rng.randint(40, 120))]
        at = rng.randrange(len(lines) + 1)
        lines[at:at] = burst
        faults["info_flood"] += 1
    if rng.random() < 0.3:
        at = rng.randrange(len(lines) + 1)
        lines.insert(at, make("\x00\x01\x02binary junk\x00", "junk"))
        faults["binary_junk"] += 1
    if rng.random() < 0.3:
        at = rng.randrange(len(lines) + 1)
        chatter = "info:  a long line of game chatter about /home/x/game/file.bin " * 300
        lines.insert(at, make(chatter, "layer"))
        faults["huge_line"] += 1
    if rng.random() < 0.02:
        # One long *token* (16 KB would take seconds in events.mask; measured in the report).
        at = rng.randrange(len(lines) + 1)
        lines.insert(at, make("A" * 2000, "junk"))
        faults["token_bomb"] += 1
    lines.append(make(rng.choice(PRESENTER_LINES), "presenter"))
    return lines


def _marker(line: _Line) -> str:
    return line.text.rsplit(" ", 1)[-1]


def _in_log(line: _Line, text: str) -> bool:
    """Whether ``line`` reached the run log; a line over the length cap keeps its head."""
    if len(line.text) > workers.LOG_LINE_CHARS:
        return line.text[:60] in text and "chars cut>" in text
    return _marker(line) in text


def _run_log_world(
    seed: int, rng: random.Random, logs_dir: Path, faults: Counter, stats: Counter
) -> None:
    profile = f"sim{seed}"
    worker = workers.BackendWorker(Config(), profile, "launch")
    fed: list[str] = []
    worker._window_watch = SimpleNamespace(feed=fed.append)
    lines = _log_stream(seed, rng, faults)
    for line in lines:
        worker._on_event(Event("log", line.text))
    worker._window_watch = None
    worker._on_event(Event("log", rng.choice(LAYER_NOISE_LINES)))
    worker._close_log()
    stats["log_worlds"] += 1
    stats["log_lines"] += len(lines)
    folder = logs_dir / runlog.game_id(profile)
    path = next(folder.glob("launch-*.log"), None)
    if path is None:
        raise WorldFailed(
            seed, "log", "log-written", f"no log under {folder}", f"lines={len(lines)}"
        )
    text = path.read_text(encoding="utf-8")
    presenters = [line for line in lines if launch_window.is_first_frame(line.text)]
    layer = [
        line
        for line in lines
        if _INFO_PREFIX.match(line.text) and not launch_window.is_first_frame(line.text)
    ]
    kept = [line for line in layer if _in_log(line, text)]
    if len(kept) > workers.LAYER_INFO_LINES:
        raise WorldFailed(
            seed,
            "log",
            "log-cap",
            f"{len(kept)} layer lines kept, cap is {workers.LAYER_INFO_LINES}",
            f"lines={len(lines)} layer={len(layer)}",
        )
    expected = min(workers.LAYER_INFO_LINES, len(layer))
    if len(kept) != expected:
        missing = [line for line in layer if not _in_log(line, text)][:3]
        raise WorldFailed(
            seed,
            "log",
            "log-cap-exact",
            f"kept {len(kept)}, expected {expected}; first missing {missing}",
            f"lines={len(lines)} layer={len(layer)}",
        )
    # The first PRESENTER_LINES presenter lines are kept, outside the info cap; later ones
    # (resizes) are dropped so a resize loop cannot flood the log.
    for index, line in enumerate(presenters):
        if (_in_log(line, text)) != (index < workers.PRESENTER_LINES):
            raise WorldFailed(
                seed,
                "log",
                "presenter-kept",
                f"#{index}: {line.text[:80]}",
                f"lines={len(lines)} presenters={len(presenters)}",
            )
    for line in lines:
        if line.kind in ("problem", "own") and not _in_log(line, text):
            raise WorldFailed(
                seed, "log", "log-keeps-errors-and-own-lines", line.text[:80], f"lines={len(lines)}"
            )
    # Every launch line is offered to the first-frame watch, including the ones the
    # cap then drops from the log (the flood in the middle must not starve it).
    wanted = [line.text for line in lines]
    if fed != wanted:
        raise WorldFailed(
            seed,
            "log",
            "every-presenter-reaches-the-watch",
            f"fed {len(fed)} of {len(wanted)} launch lines",
            f"lines={len(lines)}",
        )


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------
def run_worlds(seeds, root: Path, logs_dir: Path) -> dict:
    seeds = list(seeds)
    summary = {"worlds": 0, "seeds": seeds, "faults": Counter(), "stats": Counter(), "failures": []}

    def record(seed: int, part: str, run) -> None:
        try:
            run()
        except WorldFailed as exc:
            summary["failures"].append(exc)
        except Exception:  # noqa: BLE001 - a simulator bug, reported as one
            summary["failures"].append(
                WorldFailed(seed, part, "simulator", traceback.format_exc(limit=6), "-")
            )

    for seed in seeds:
        summary["worlds"] += 1
        watch = _WatchWorld(seed, random.Random(seed), root / f"w{seed}")
        log_faults: Counter = Counter()
        log_stats: Counter = Counter()
        record(seed, "watch", watch.run)
        record(
            seed,
            "log",
            lambda seed=seed, faults=log_faults, stats=log_stats: _run_log_world(
                seed, random.Random(seed + 1), logs_dir, faults, stats
            ),
        )
        summary["faults"].update(watch.faults)
        summary["faults"].update(log_faults)
        summary["stats"].update(watch.stats)
        summary["stats"].update(log_stats)
        if len(summary["failures"]) >= 20:
            break
    if summary["failures"]:
        raise AssertionError(
            f"{len(summary['failures'])} world failure(s) after {summary['worlds']} worlds:\n\n"
            + "\n\n".join(str(failure) for failure in summary["failures"][:10])
        )
    return summary


def _seeds(count: int) -> list[int]:
    only = os.environ.get("SIM_SEED")
    if only:
        return [int(only)]
    return list(range(count))


def _run_sim(count: int, tmp_path: Path, monkeypatch) -> dict:
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: logs)
    summary = run_worlds(_seeds(count), tmp_path / "worlds", logs)
    report = summary["faults"]
    if os.environ.get("SIM_SUMMARY"):
        Path(os.environ["SIM_SUMMARY"]).write_text(
            f"worlds={summary['worlds']} faults={dict(report)} stats={dict(summary['stats'])}\n",
            encoding="utf-8",
        )
    print(f"\nsim worlds={summary['worlds']} faults={dict(report)}")
    print(f"sim stats={dict(summary['stats'])}")
    return summary


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------
def test_the_catalogue_is_what_the_code_accepts(tmp_path):
    """Every confirmed presenter line fires; every near miss does not."""
    for line in PRESENTER_LINES:
        evidence = launch_window.WindowEvidence(set(), proc_root=tmp_path / "gone")
        evidence.feed(line)
        assert evidence.seen([], 0.0, {}), line
    for line in NEAR_MISS_LINES:
        evidence = launch_window.WindowEvidence(set(), proc_root=tmp_path / "gone")
        evidence.feed(line)
        assert not evidence.seen([], 0.0, {}), line
    for line in DECIMAL_STAMP_LINES:
        evidence = launch_window.WindowEvidence(set(), proc_root=tmp_path / "gone")
        evidence.feed(line)
        assert evidence.seen([], 0.0, {}), line


def test_the_cap_and_the_detector_agree_on_the_shared_prefix():
    """A decimal timestamp is a layer line *and* a first frame: one grammar for both."""
    for line in DECIMAL_STAMP_LINES:
        assert workers._LAYER_INFO_RE.match(line)
        assert launch_window._FIRST_FRAME_RE.match(line)
    for line in PRESENTER_LINES:
        assert workers._LAYER_INFO_RE.match(line)
        assert launch_window._FIRST_FRAME_RE.match(line)


def test_a_failing_world_names_its_seed_and_a_repro():
    message = str(WorldFailed(7, "watch", "presenter-line-fires", "nothing fired", "step=1"))
    assert "seed=7" in message
    assert "SIM_SEED=7" in message
    assert "presenter-line-fires" in message


def test_twenty_world_seeds_always_run(tmp_path, monkeypatch):
    summary = _run_sim(20, tmp_path, monkeypatch)
    assert summary["worlds"] == len(summary["seeds"])
    if len(summary["seeds"]) > 1:
        assert summary["faults"]["stop"] + summary["faults"]["hide"] > 0


@pytest.mark.skipif(
    os.environ.get("NADARE_SIM") != "1",
    reason="1,000-world run: set NADARE_SIM=1 (and optionally SIM_SEEDS)",
)
def test_one_thousand_worlds(tmp_path, monkeypatch):
    count = int(os.environ.get("SIM_SEEDS", "1000"))
    summary = _run_sim(count, tmp_path, monkeypatch)
    assert summary["worlds"] == len(summary["seeds"])


def test_a_raising_process_rule_does_not_end_the_watch(monkeypatch):
    """One transient ``game_pids()`` error (a /proc read, say) must not lose the watch.

    The monitor logs it once and keeps polling; the presenter line still ends the sheet
    (SIM F3, fixed in the #1821 follow-up).
    """
    monkeypatch.setattr(launch_window, "x11_windows", lambda: {})
    monkeypatch.setattr(launch_window, "POLL_INTERVAL", 0.005)

    def pids():
        raise OSError("simulated /proc failure")

    emitted = []
    with launch_window.watch(pids, emitted.append, Cancel()) as watched:
        time.sleep(0.05)
        watched.feed(PRESENTER_LINES[0])
        deadline = time.monotonic() + 2
        while not emitted and time.monotonic() < deadline:
            time.sleep(0.01)
    assert [event.kind for event in emitted] == ["window_seen"]
    assert not any(
        thread.name == "launch-window" and thread.is_alive() for thread in threading.enumerate()
    )


def test_a_presenter_after_a_log_flood_still_ends_the_sheet(tmp_path, monkeypatch):
    """The cap drops the line from the log *after* feeding it to the watch (#1821)."""
    logs = tmp_path / "logs"
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: logs)
    monkeypatch.setattr(launch_window, "x11_windows", lambda: {})
    monkeypatch.setattr(launch_window, "POLL_INTERVAL", 0.01)
    real_evidence = launch_window.WindowEvidence

    def evidence(baseline, **kwargs):
        return real_evidence(baseline, proc_root=tmp_path / "gone", **kwargs)

    monkeypatch.setattr(launch_window, "WindowEvidence", evidence)
    worker = workers.BackendWorker(Config(), "g", "launch")
    events = []
    worker.backend_event.connect(events.append, Qt.ConnectionType.DirectConnection)

    def play(_backend):
        for index in range(2000):
            worker._on_event(Event("log", f"info:  Presenter: Got -1000208000 {index}"))
        worker._on_event(Event("log", PRESENTER_LINES[0]))
        deadline = time.monotonic() + 2
        while not any(event.kind == "window_seen" for event in events):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        return 0

    monkeypatch.setattr(workers.launchgate, "play", play)
    backend = SimpleNamespace(game_pids=lambda: lambda: [10])
    assert worker._dispatch(backend) == 0
    assert [event.kind for event in events if event.kind == "window_seen"] == ["window_seen"]
    text = next((logs / "g").glob("launch-*.log")).read_text(encoding="utf-8")
    assert text.count("Got -1000208000") <= workers.LAYER_INFO_LINES
