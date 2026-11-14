# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Background status probes of every profile after start-up.

Split out of :mod:`nadare.gui.main_window`; the code is unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("nadare.gui.main_window")

#: Most profiles probed at once at startup; the rest follow as each one ends (#388 F-07).
STARTUP_PROBE_LIMIT = 3

#: How long one process-list snapshot is shared by startup probes before a fresh one is taken.
PROBE_ROUND_MAX_AGE = 2.0

#: A startup probe that reports nothing for this long stops holding one of the probe slots.
STARTUP_PROBE_TIMEOUT = 60.0


class ProbesMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    # ---- probing ------------------------------------------------------------
    def _probe_all(self) -> None:
        profiles = [profile for profile in self.config.profiles if profile != self._current_profile]
        if not profiles:
            return
        self._probe_round = None
        self._probe_queue.extend(profiles)
        self._pump_probes()

    def _probe_scan_round(self) -> Any:
        """The process-list snapshot for a probe: shared within a burst, never long-lived.

        Probes held back by the limit can start seconds later; reusing the first snapshot
        would report a game that started or ended in between with its old state.
        """
        from ...core.process import ProcessScanRound

        now = time.monotonic()
        if self._probe_round is None or now - self._probe_round_at >= PROBE_ROUND_MAX_AGE:
            self._probe_round = ProcessScanRound()
            self._probe_round_at = now
        return self._probe_round

    def _pump_probes(self) -> None:
        """Start queued startup probes, never more than the limit at once."""
        now = time.monotonic()
        for stuck in [p for p, t in self._probe_active.items() if now - t >= STARTUP_PROBE_TIMEOUT]:
            del self._probe_active[stuck]  # it never reported back: do not let it hold the queue
        while self._probe_queue and len(self._probe_active) < STARTUP_PROBE_LIMIT:
            profile = self._probe_queue.popleft()
            if not self._start(profile, "status", payload=self._probe_scan_round(), silent=True):
                continue
            self._probe_active[profile] = now

    def _probe_finished(self, profile: str) -> None:
        """Make room for the next startup probe after ``profile``'s status ended."""
        if profile in self._probe_active:
            del self._probe_active[profile]
            self._pump_probes()
