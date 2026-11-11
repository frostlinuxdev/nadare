# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Remaining-time estimate from a progress fraction.

Install progress reaches the GUI only as a fraction (0..1). :class:`EtaEstimator` smooths the
rate of change so one slow or fast second does not swing the number, stays silent until it has
enough samples, and starts over when progress restarts (a new phase) or goes quiet for a while.
"""

from __future__ import annotations

MIN_SAMPLES = 8
MIN_SPAN = 10.0
#: Seconds of recent progress the rate is measured over.
WINDOW = 20.0
#: Seconds without an update after which the old rate no longer describes the transfer.
GAP_RESET = 60.0
#: A drop larger than this means a new phase began at zero.
RESTART_DROP = 0.02
#: Estimates beyond this are noise, not information.
MAX_ETA = 48 * 3600


class EtaEstimator:
    """Rate over the last :data:`WINDOW` seconds of progress.

    The jump between the first two samples is not a transfer rate: a resumed install skips the
    files already on disk in one step. It is used as the starting point and not counted, or the
    estimate would stay far too short for as long as that jump stays in the window.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._points: list[tuple[float, float]] = []
        self._seen = 0
        self._started: float | None = None

    def update(self, fraction: float, now: float) -> float | None:
        """Record ``fraction`` at time ``now`` (seconds); return seconds left or ``None``."""
        if self._points:
            last_time, last_fraction = self._points[-1]
            if fraction < last_fraction - RESTART_DROP or now - last_time > GAP_RESET:
                self.reset()
        self._seen += 1
        if self._seen == 2:
            self._points = []  # the first jump is a baseline, not a rate
        if not self._points:
            self._started = now
        self._points.append((now, fraction))
        while len(self._points) > 1 and now - self._points[0][0] > WINDOW:
            self._points.pop(0)
        return self._eta(fraction)

    def _eta(self, fraction: float) -> float | None:
        assert self._started is not None
        first_time, first_fraction = self._points[0]
        last_time = self._points[-1][0]
        span = last_time - first_time
        if self._seen < MIN_SAMPLES or last_time - self._started < MIN_SPAN or span <= 0:
            return None
        rate = (fraction - first_fraction) / span
        if rate <= 0 or fraction >= 1.0:
            return None
        return (1.0 - fraction) / rate


def format_eta(seconds: float | None) -> str | None:
    """Human wording for a remaining time, or ``None`` when there is nothing useful to say."""
    if seconds is None or seconds > MAX_ETA:
        return None
    if seconds < 60:
        return "less than a minute left"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"about {minutes} min left"
    hours, minutes = divmod(minutes, 60)
    if minutes == 0:
        return f"about {hours} h left"
    return f"about {hours} h {minutes} min left"


#: Seconds of recent bytes the speed is measured over.
SPEED_WINDOW = 5.0
#: Shortest span a speed is reported for; anything shorter is timer noise.
MIN_SPEED_SPAN = 0.2


class SpeedMeter:
    """Bytes per second over the last :data:`SPEED_WINDOW` seconds of a growing byte counter.

    Starts over when the counter drops (a new phase) or goes quiet for :data:`GAP_RESET`, and
    says nothing until two samples exist.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._points: list[tuple[float, float]] = []

    def update(self, done: float, now: float) -> float | None:
        if self._points:
            last_time, last_done = self._points[-1]
            if done < last_done or now - last_time > GAP_RESET:
                self.reset()
        self._points.append((now, done))
        # Keep the newest sample at or past the window's edge. Events queued while the GUI
        # was busy arrive microseconds apart; measuring them against each other alone turned
        # a 70 MiB/s download into "6 GiB/s" (#1133). The sample from before the pause keeps
        # the span honest.
        while len(self._points) > 2 and now - self._points[1][0] >= SPEED_WINDOW:
            self._points.pop(0)
        first_time, first_done = self._points[0]
        span = now - first_time
        if len(self._points) < 2 or span < MIN_SPEED_SPAN:
            return None
        return (done - first_done) / span


def format_speed(bytes_per_second: float | None) -> str:
    """``85.0 MiB/s``; empty when there is no speed to show."""
    if not bytes_per_second or bytes_per_second <= 0:
        return ""
    value = float(bytes_per_second)
    if value < 1024:
        return f"{value:.0f} B/s"
    if value < 1024**2:
        return f"{value / 1024:.1f} KiB/s"
    if value < 1024**3:
        return f"{value / 1024**2:.1f} MiB/s"
    return f"{value / 1024**3:.2f} GiB/s"


__all__ = ["EtaEstimator", "SpeedMeter", "format_eta", "format_speed"]
