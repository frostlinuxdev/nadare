# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Runner downloads on the Downloads page (#1625), and the per-row transfer rate (#1623).

A runner fetched by Play or an install belongs under that game's row; a background prefetch
stands alone.  Backend events arrive through the controller like game events, and the prefetch
thread reports through :class:`MainWindow`'s ``runner_download_event`` signal, so no widget is
ever touched off the GUI thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QTimer

from ...core import runners as core_runners
from ...core.diskspace import human_size
from ...core.eta import SpeedMeter, format_eta, format_speed
from ...core.events import Cancel, Event
from ..downloads_page import DownloadRow
from ..i18n import tr_n

#: How long a finished runner row stays before it leaves the list.
FINISHED_ROW_MS = 4000
#: Seconds of recent bytes the Downloads page measures a rate over (#1623).
SPEED_WINDOW = 3.0


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _connections(data: dict) -> int:
    count = data.get("connections")
    return int(count) if isinstance(count, (int, float)) and count > 0 else 0


def _transfer_text(rate: float | None, data: dict, eta: str = "") -> str:
    """``85.0 MiB/s · 4 connections · about 2 min left``, with only what is known."""
    parts: list[str] = []
    speed = format_speed(rate)
    if speed:
        parts.append(speed)
        count = _connections(data)
        if count:
            parts.append(tr_n("{n} connections", count))
    if eta:
        parts.append(eta)
    return " · ".join(parts)


def _eta_text(remaining: float, rate: float | None) -> str:
    if rate and rate > 0 and remaining > 0:
        return format_eta(remaining / rate) or ""
    return ""


def _runner_title(runner_id: str, tag: str) -> str:
    """``dwproton`` + ``dwproton-11.0-17`` -> ``DW-Proton 11.0-17``."""
    spec = core_runners.RUNNERS.get(runner_id)
    name = spec.name if spec is not None else runner_id.capitalize()
    short = tag
    # Longest prefix first: "ge" is a prefix of "GE-Proton11-7", and stripping it would
    # leave "-Proton11-7" (the family name is the useful part).
    for prefix in (name, runner_id):
        if prefix and short.lower().startswith(prefix.lower()):
            short = short[len(prefix):].lstrip("-_ ")
            break
    return f"{name} {short}".strip()


@dataclass
class _RunnerDownload:
    key: str
    title: str
    parent: str = ""  # the game whose install/Play needed it; empty for a prefetch
    sources: set[str | Cancel] = field(default_factory=set)
    state: str = "running"
    detail: str = ""
    fraction: float | None = None
    eta: str = ""
    total: float = 0
    done: float = 0
    meter: SpeedMeter = field(default_factory=lambda: SpeedMeter(SPEED_WINDOW))

    def row(self) -> DownloadRow:
        tip = (
            "Stop the download that needs this runner."
            if self.parent
            else "Stop fetching this runner. Nadare tries again next time."
        )
        return DownloadRow(
            self.key,
            self.title,
            "runner",
            self.state,
            detail=self.detail,
            fraction=self.fraction,
            eta=self.eta,
            can_pause=False,
            parent_profile=self.parent,
            cancel_tip=tip,
        )


class RunnerDownloadsMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    def _init_runner_downloads(self) -> None:
        self._runner_rows: dict[str, _RunnerDownload] = {}
        #: Keys a Cancel just removed: a late event from the stopping thread must not revive it.
        self._runner_ignored: dict[str, set[str | Cancel]] = {}
        self._download_speed: dict[str, SpeedMeter] = {}
        self._download_totals: dict[str, float] = {}
        self._download_eta_text: dict[str, str] = {}
        self._runner_prefetch_cancel: Cancel | None = None
        self._runner_prefetch_thread = None

    # ---- runner rows --------------------------------------------------------
    def _on_runner_download_event(self, source: object, event: Event) -> bool:
        """Record a runner event; ``source`` is the game profile or the prefetch's Cancel.

        Returns True when the event was a runner event, so the game row does not also show
        the runner's bytes as its own progress.
        """
        data = event.data or {}
        token = source if isinstance(source, Cancel) else None
        if event.kind == "runner_end":
            self._end_prefetch(token)
            return True
        runner_id = str(data.get("runner") or "")
        tag = str(data.get("tag") or "")
        if not runner_id or not tag:
            return False

        key = f"runner:{tag}"
        if (token is not None and token.cancelled) or (
            source in self._runner_ignored.get(key, set())
        ):
            return True  # the user just stopped this one
        entry = self._runner_rows.get(key)
        structural = entry is None
        if entry is None:
            if event.kind in ("done", "error"):
                return True  # an already installed runner reporting in: no row to show
            entry = _RunnerDownload(key, _runner_title(runner_id, tag))
            self._runner_rows[key] = entry
        if token is not None:
            entry.sources.add(token)
        elif isinstance(source, str) and source:
            entry.sources.add(source)
            structural = structural or entry.parent != source
            entry.parent = source

        total = _number(data.get("total"))
        if total is not None and total > 0:
            entry.total = total
        done = _number(data.get("done"))
        fraction = event.progress
        if fraction is not None:
            fraction = max(0.0, min(1.0, float(fraction)))
        if done is None and fraction is not None and entry.total > 0:
            done = fraction * entry.total
        if done is not None:
            entry.done = done
            if entry.total > 0:
                fraction = max(0.0, min(1.0, done / entry.total))
        if fraction is not None:
            entry.fraction = fraction

        if event.kind == "error":
            self._remove_runner_row(key)
            return True
        if event.kind == "done":
            if data.get("runner_finished"):
                self._finish_runner(entry)
            elif event.message:
                entry.detail = event.message
                self._runner_changed(entry)
            return True

        if event.kind in ("status", "progress"):
            entry.detail = self._runner_detail(event, entry, done)
            if data.get("stage") == "extract":
                entry.eta = ""
                entry.meter.reset()
            elif done is not None:
                rate = entry.meter.update(done, time.monotonic())
                entry.eta = _transfer_text(rate, data, _eta_text(entry.total - done, rate))
            self._runner_changed(entry, structural=structural)
        elif structural:
            self._runner_changed(entry)
        return True

    @staticmethod
    def _runner_detail(event: Event, entry: _RunnerDownload, done: float | None) -> str:
        """The row's status line, in the app's own words and size format."""
        data = event.data or {}
        if data.get("stage") == "extract":
            return "Unpacking"
        if done is not None and entry.total > 0:
            return f"Downloading {human_size(done)} of {human_size(entry.total)}"
        if event.kind == "status" and entry.total > 0:
            return f"Downloading {human_size(entry.total)}"
        return event.message or entry.detail

    def _finish_runner(self, entry: _RunnerDownload) -> None:
        if entry.state == "finished":
            return
        entry.state = "finished"
        entry.detail = "Finished"
        entry.fraction = 1.0
        entry.eta = ""
        QTimer.singleShot(FINISHED_ROW_MS, lambda key=entry.key: self._remove_runner_row(key))
        self._runner_changed(entry)

    def _remove_runner_row(self, key: str) -> None:
        if self._runner_rows.pop(key, None) is None:
            return
        if not self._closing:
            self._refresh_downloads()

    def _runner_changed(
        self, entry: _RunnerDownload | None = None, *, structural: bool = True
    ) -> None:
        """Redraw the page: a new or finished row, or just one row's moving numbers."""
        if self._closing:
            return
        if structural or entry is None:
            self._refresh_downloads()
        elif self.stack.currentWidget() is self.downloads:
            self.downloads.update_progress(entry.key, entry.fraction, entry.detail, entry.eta)

    def _end_runner_downloads(self, profile: str) -> None:
        """The game's operation ended: drop its live runner rows, keep a finished one's timer."""
        self._end_runner_source(profile)

    def _end_prefetch(self, token: Cancel | None) -> None:
        """The download going with ``token`` is over; finished rows still linger for a moment."""
        self._end_runner_source(token)

    def _end_runner_source(self, source: str | Cancel | None) -> None:
        for key, held in list(self._runner_ignored.items()):
            held.discard(source)
            if not held:
                del self._runner_ignored[key]
        changed = False
        for key, entry in list(self._runner_rows.items()):
            if source not in entry.sources:
                continue
            entry.sources.discard(source)
            if entry.parent == source:
                entry.parent = next((s for s in entry.sources if isinstance(s, str)), "")
            if not entry.sources and entry.state != "finished":
                self._runner_rows.pop(key)
            changed = True
        if changed and not self._closing:
            self._refresh_downloads()

    def _group_runner_rows(self, rows: list[DownloadRow]) -> list[DownloadRow]:
        """Put each game's runner directly under it; a row with no game row stands alone."""
        if not self._runner_rows:
            return rows
        groups: dict[str, list[DownloadRow]] = {}
        for entry in self._runner_rows.values():
            groups.setdefault(entry.parent, []).append(entry.row())
        grouped: list[DownloadRow] = []
        for row in rows:
            grouped.append(row)
            grouped.extend(groups.pop(row.profile, []))
        for extra in groups.values():
            for runner in extra:
                runner.parent_profile = ""  # not under a game row: no indent
            grouped.extend(extra)
        return grouped

    def _runner_active_count(self) -> int:
        return sum(1 for entry in self._runner_rows.values() if entry.state != "finished")

    # ---- cancel -------------------------------------------------------------
    def _cancel_download_row(self, profile: str) -> None:
        """Cancel a runner row, or hand a game's row to the usual "stop, keep files" choice."""
        if not profile.startswith("runner:"):
            self._cancel_with_choice(profile)
            return
        entry = self._runner_rows.get(profile)
        if entry is None:
            return
        tokens = [source for source in entry.sources if isinstance(source, Cancel)]
        for source in entry.sources:
            if isinstance(source, Cancel):
                source.cancel()
            else:
                self.controller.cancel(source)
        if tokens:
            self._runner_ignored.setdefault(profile, set()).update(entry.sources)
            self._remove_runner_row(profile)  # gone at once; a late event must not revive it
            return
        if entry.state != "finished":
            entry.detail = "Stopping…"
            self._runner_changed(entry)

    # ---- per-row speed (#1623) ---------------------------------------------
    def _download_transfer_text(
        self, profile: str, event: Event, fraction: float | None, eta: str
    ) -> str:
        """Add ``85.0 MiB/s · 4 connections`` to an install row's ETA line."""
        data = event.data or {}
        total = _number(data.get("total"))
        if total is not None and total > 0:
            self._download_totals[profile] = total
        if event.kind != "progress":
            return eta
        done = _number(data.get("done"))
        if done is None and fraction is not None:
            known = self._download_totals.get(profile, 0)
            done = fraction * known if known > 0 else None
        if done is None:
            return eta
        meter = self._download_speed.setdefault(profile, SpeedMeter(SPEED_WINDOW))
        rate = meter.update(done, time.monotonic())
        speed = _transfer_text(rate, data)
        if eta and speed:
            return f"{speed} · {eta}"
        return speed or eta

    def _reset_download_speed(self, profile: str) -> None:
        self._download_speed.pop(profile, None)
        self._download_totals.pop(profile, None)
        self._download_eta_text.pop(profile, None)
