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
#: Seconds without an update after which the old rate no longer describes the transfer.
GAP_RESET = 60.0
#: A drop larger than this means a new phase began at zero.
RESTART_DROP = 0.02
ALPHA = 0.15
#: Estimates beyond this are noise, not information.
MAX_ETA = 48 * 3600


class EtaEstimator:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._first: tuple[float, float] | None = None
        self._last: tuple[float, float] | None = None
        self._rate: float | None = None
        self._samples = 0

    def update(self, fraction: float, now: float) -> float | None:
        """Record ``fraction`` at time ``now`` (seconds); return seconds left or ``None``."""
        if self._last is not None:
            last_time, last_fraction = self._last
            if fraction < last_fraction - RESTART_DROP or now - last_time > GAP_RESET:
                self.reset()
        if self._last is None:
            self._first = self._last = (now, fraction)
            self._samples = 1
            return None
        last_time, last_fraction = self._last
        delta = now - last_time
        if delta <= 0:
            return self._eta(fraction)
        instant = max(0.0, fraction - last_fraction) / delta
        self._rate = instant if self._rate is None else ALPHA * instant + (1 - ALPHA) * self._rate
        self._last = (now, fraction)
        self._samples += 1
        return self._eta(fraction)

    def _eta(self, fraction: float) -> float | None:
        assert self._first is not None and self._last is not None
        if self._samples < MIN_SAMPLES or self._last[0] - self._first[0] < MIN_SPAN:
            return None
        if not self._rate or self._rate <= 0 or fraction >= 1.0:
            return None
        return (1.0 - fraction) / self._rate


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


__all__ = ["EtaEstimator", "format_eta"]
