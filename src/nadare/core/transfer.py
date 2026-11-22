# SPDX-License-Identifier: GPL-3.0-or-later
"""Process-wide download budget and per-sync transfer measurements."""

from __future__ import annotations

import fcntl
import functools
import inspect
import json
import logging
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .events import Event


class TokenBucket:
    """One small burst budget shared by every HTTP reader; MB means 1,000,000 bytes."""

    def __init__(self):
        self._condition = threading.Condition()
        self.limit_mb_s = 0.0
        self._tokens = 0.0
        self._updated = time.monotonic()
        self._polled = 0.0
        self.path = None

    def attach(self, path):
        """Share one budget with download child processes, guarded by flock.

        Best effort: a budget file that cannot be written (a read-only config
        folder) leaves an in-memory bucket instead of breaking a download. The
        rate currently in the file is adopted, so a child started by a limited
        parent keeps sharing it.
        """
        self.path = Path(path)
        try:
            with self._state() as state:
                self.limit_mb_s = float(state[0])
        except (OSError, ValueError, TypeError):
            self.path = None

    @contextmanager
    def _state(self):
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as handle:
            # The critical section is one small read/write. Wait for it, but never
            # forever: a wedged holder falls back to this process's own rate.
            deadline = time.monotonic() + 0.5
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.005)
            try:
                raw = handle.read()
                state = json.loads(raw) if raw else [0.0, 0.0, time.monotonic()]
                if not (isinstance(state, list) and len(state) == 3
                        and all(isinstance(v, (int, float)) and math.isfinite(v)
                                and v >= 0 for v in state)
                        and state[2] <= time.monotonic()):
                    raise ValueError(raw)
            except (ValueError, TypeError):
                state = [self.limit_mb_s, 0.0, time.monotonic()]
            yield state
            handle.seek(0)
            json.dump(state, handle)
            handle.truncate()

    def set_limit(self, mb_s):
        try:
            value = float(mb_s)
        except (ValueError, TypeError):
            value = 0.0
        if not math.isfinite(value) or value < 0:
            value = 0.0
        with self._condition:
            if self.path is not None:
                try:
                    with self._state() as state:
                        if state[0] != value:
                            state[:] = [value, 0.0, time.monotonic()]
                except OSError:
                    self.path = None  # no shared budget; the memory one still works
            # This process already knows the new rate: an unlimited download must
            # not open the file again until a child could have changed it.
            self._polled = time.monotonic() + 0.5
            if value == self.limit_mb_s:
                return
            self.limit_mb_s = value
            self._tokens = 0.0
            self._updated = time.monotonic()
            self._condition.notify_all()

    def take(self, amount, cancel=None):
        remaining = amount
        while remaining:
            if cancel is not None:
                cancel.raise_if_cancelled()
            with self._condition:
                now = time.monotonic()
                # A limited transfer reads the shared file per block (it is slow
                # anyway). An unlimited one only polls it twice a second, so the
                # default "no limit" never pays a lock and JSON parse per 64 KiB
                # while a child process can still see a newly set limit.
                if self.path is not None and (self.limit_mb_s > 0 or now >= self._polled):
                    try:
                        with self._state() as state:
                            now = time.monotonic()
                            self.limit_mb_s = float(state[0])
                            rate = state[0] * 1_000_000
                            if not rate:
                                self._polled = now + 0.5
                                return
                            tokens = min(rate * 0.1, state[1] + (now - state[2]) * rate)
                            used = min(remaining, tokens)
                            remaining -= used
                            state[:] = [state[0], tokens - used, now]
                        if remaining:
                            self._condition.wait(min(0.1, remaining / rate))
                        continue
                    except OSError:
                        self.path = None  # the file went away; keep the in-memory rate
                rate = self.limit_mb_s * 1_000_000
                if not rate:
                    return
                now = time.monotonic()
                self._tokens = min(rate * 0.1, self._tokens + (now - self._updated) * rate)
                self._updated = now
                used = min(remaining, self._tokens)
                remaining -= used
                self._tokens -= used
                if remaining:
                    self._condition.wait(min(0.1, remaining / rate))


limiter = TokenBucket()
if os.environ.get("NADARE_DOWNLOAD_BUDGET"):
    limiter.attach(os.environ["NADARE_DOWNLOAD_BUDGET"])
_local = threading.local()


def current_transfer():
    return getattr(_local, "transfer", None)


def inherit_transfer(transfer):
    _local.transfer = transfer


class Measurement:
    def __init__(self, workers):
        self.workers = workers
        self.lock = threading.Lock()
        self.active = self.opened = self.bytes = 0
        self.hosts = set()
        self.started = time.monotonic()

    def opened_response(self, url, opened):
        with self.lock:
            self.active += 1
            self.opened += int(opened)
            self.hosts.add(urlsplit(url).hostname or "")

    def summary(self):
        seconds = max(time.monotonic() - self.started, 1e-9)
        return {
            "download_summary": True,
            "workers_requested": self.workers,
            "connections_opened": self.opened,
            "hosts": sorted(self.hosts),
            "bytes": self.bytes,
            "seconds": seconds,
            "average_mib_s": self.bytes / seconds / (1 << 20),
        }


@contextmanager
def measure(workers, emit):
    previous = current_transfer()
    measurement = Measurement(workers)
    inherit_transfer(measurement)
    try:
        yield measurement
    finally:
        inherit_transfer(previous)
        data = measurement.summary()
        message = (
            f"Download sync: workers requested={workers}, "
            f"connections opened={data['connections_opened']}, "
            f"hosts={','.join(data['hosts']) or '-'}, bytes={data['bytes']}, "
            f"seconds={data['seconds']:.3f}, average MiB/s={data['average_mib_s']:.3f}"
        )
        logging.getLogger(__name__).info(message)
        if emit is not None:
            emit(Event("log", message, data=data))


class Response:
    """Keep the response API while charging bounded reads to the shared bucket."""

    def __init__(self, response, url, opened=True):
        self._response = response
        self._measurement = current_transfer()
        self._active = True
        self.cancel = None
        if self._measurement is not None:
            self._measurement.opened_response(url, opened)

    def __getattr__(self, name):
        return getattr(self._response, name)

    def read(self, amount=-1):
        blocks = []
        remaining = amount
        while remaining:
            if self.cancel is not None:
                self.cancel.raise_if_cancelled()
            block = self._response.read(min(remaining, 65536) if remaining > 0 else 65536)
            if not block:
                self._finished()
                break
            limiter.take(len(block), self.cancel)
            if self._measurement is not None:
                with self._measurement.lock:
                    self._measurement.bytes += len(block)
            blocks.append(block)
            if remaining > 0:
                remaining -= len(block)
        return b"".join(blocks)

    def _finished(self):
        if self._active:
            self._active = False
            if self._measurement is not None:
                with self._measurement.lock:
                    self._measurement.active -= 1

    def close(self):
        try:
            close = getattr(self._response, "close", None)
            if close is not None:
                close()
        finally:
            self._finished()

    def __enter__(self):
        enter = getattr(self._response, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *exc):
        leave = getattr(self._response, "__exit__", None)
        if leave is None:
            self.close()
            return False
        try:
            return leave(*exc)
        finally:
            self._finished()


def measured(function):
    """Measure standalone syncs, sharing the outer sync when nested."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        if current_transfer() is not None:
            return function(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        with measure(bound.arguments.get("workers", 1), bound.arguments.get("emit")):
            return function(*args, **kwargs)

    return wrapped
