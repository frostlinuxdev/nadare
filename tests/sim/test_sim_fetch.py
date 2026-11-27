# SPDX-License-Identifier: GPL-3.0-or-later
"""Seeded world simulation of the #1816 publisher-index fetch retries.

A *world* is one ``sources.default_fetch`` call against ``download.http_get`` replaced by a
scripted fake transport and ``download.wait`` replaced by a fake that moves a fake clock (no
real sleep). Everything is generated from ``random.Random(seed)`` alone, so a seed is the
whole reproduction: fault script, attempt limit, Stop point, token source, Retry-After shape.

The oracle is written independently of the code under test: it hard-codes which faults are
transient, the 30 s wait cap, and the backoff schedule; it never calls
``download.transient_status`` or ``sources.MAX_FETCH_RETRY_AFTER``.

``NADARE_SIM=1`` runs ``SIM_SEEDS`` worlds (1,000 by default); 20 seeds always run.
"""

from __future__ import annotations

import http.client
import logging
import math
import os
import random
import socket
import ssl
import urllib.error
from dataclasses import dataclass

import pytest

from nadare.core import directdl, download, sources
from nadare.core.events import Cancel, Cancelled

SEEDS = int(os.environ.get("SIM_SEEDS", "1000"))
BODY = b'{"ok": true}'
HOST = "api.example.com"
URL = f"https://{HOST}/publisher/index.json?key=TOPSECRET"
SAFE = f"https://{HOST}/publisher/index.json"

#: Every fault kind a world may inject.
FAULTS = (
    "ok",
    "timeout",
    "reset",
    "refused",
    "truncated",
    "truncated_wrapped",
    "http 400",
    "http 401",
    "http 403",
    "http 404",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "cert",
    "size_cap",
)
FAULT_WEIGHTS = (
    ("timeout", 6),
    ("reset", 5),
    ("refused", 4),
    ("truncated", 4),
    ("truncated_wrapped", 2),
    ("http 429", 3),
    ("http 500", 3),
    ("http 502", 3),
    ("http 503", 3),
    ("http 504", 3),
    ("http 400", 2),
    ("http 401", 1),
    ("http 403", 1),
    ("http 404", 2),
    ("cert", 1),
    ("size_cap", 1),
)
#: The oracle's own tables (not read from the product).
TRANSIENT_STATUS = {429, 500, 502, 503, 504}
PERMANENT_STATUS = {400, 401, 403, 404}
PERMANENT = {"http 400", "http 401", "http 403", "http 404", "cert", "size_cap"}
RETRY_AFTER_SHAPES = (
    None,
    "0",
    "1",
    "7.5",
    "29",
    "30",
    "120",
    "99999",
    "Wed, 21 Oct 2026 07:28:00 GMT",
    "Friday, 21-Oct-26 07:28:00 GMT",
    "-4",
    "",
    "  5  ",
    "garbage",
    "NaN",
    "inf",
)
MAX_WAIT = 30.0
BACKOFF = (2.0, 8.0)
REASONS = {"timeout", "connection error", "network error"}


# --------------------------------------------------------------------------- the world
@dataclass
class World:
    seed: int
    retries: int
    script: dict[int, str]  # 1-based attempt -> fault kind (attempts absent from it succeed)
    retry_after: str | None
    cancel_when: str  # before / call / wait / never
    cancel_at: int  # attempt or wait number the Stop belongs to
    cancel_source: str  # arg / arg_over_context / context / none


def make_world(seed: int) -> World:
    rng = random.Random(seed)
    retries = rng.randint(1, 4)
    script: dict[int, str] = {}
    ok_at: int | None = None
    for attempt in range(1, retries + 1):
        if ok_at is None and rng.random() < 0.5:
            ok_at = attempt
        elif ok_at is None:
            script[attempt] = rng.choices(
                [kind for kind, _ in FAULT_WEIGHTS],
                weights=[weight for _, weight in FAULT_WEIGHTS],
                k=1,
            )[0]
    shape = rng.choice(RETRY_AFTER_SHAPES)
    cancel_when = rng.choices(["never", "before", "call", "wait"], weights=[55, 5, 20, 20])[0]
    cancel_at = 0
    if cancel_when == "call":
        # Only a failing attempt can carry a Stop that changes the outcome.
        faults = sorted(script)
        if not faults:
            cancel_when = "never"
        else:
            cancel_at = rng.choice(faults)
    elif cancel_when == "wait":
        # A wait exists only after a transient failure that is not the last attempt.
        waits = [
            attempt
            for attempt in sorted(script)
            if attempt < retries and script[attempt] not in PERMANENT
        ]
        if not waits:
            cancel_when = "never"
        else:
            cancel_at = rng.choice(waits)
    if cancel_when == "never":
        cancel_source = rng.choices(
            ["arg", "arg_over_context", "context", "none"], weights=[3, 2, 3, 2]
        )[0]
    else:
        cancel_source = rng.choice(["arg", "arg_over_context", "context"])
    return World(seed, retries, script, shape, cancel_when, cancel_at, cancel_source)


# --------------------------------------------------------------------------- the oracle
def expected_wait(shape: str | None, attempt: int) -> float | None:
    """The exact wait a valid ``Retry-After`` demands, else ``None`` for the schedule."""
    if shape is None:
        return None
    try:
        value = float(shape.strip())
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, MAX_WAIT)


def expectations(world: World) -> tuple[int, int, str]:
    """``(calls, waits, outcome)`` from the world alone: no product helper is consulted."""
    if world.cancel_when == "before":
        return 0, 0, "cancelled"
    calls = waits = 0
    for attempt in range(1, world.retries + 1):
        calls += 1
        kind = world.script.get(attempt)
        if kind is None:
            return calls, waits, "ok"
        if world.cancel_when == "call" and world.cancel_at == attempt:
            if kind in PERMANENT or attempt >= world.retries:
                return calls, waits, "cancelled"
            return calls, waits + 1, "cancelled"  # the aborted wait still counts
        if kind in PERMANENT or attempt >= world.retries:
            return calls, waits, "error"
        waits += 1
        if world.cancel_when == "wait" and world.cancel_at == waits:
            return calls, waits, "cancelled"
    raise AssertionError("unreachable world")  # pragma: no cover


# --------------------------------------------------------------------------- the transport
def _fault(kind: str, url: str, shape: str | None) -> BaseException:
    if kind == "timeout":
        return TimeoutError("timed out")
    if kind == "reset":
        return ConnectionResetError(104, "Connection reset by peer")
    if kind == "refused":
        exc = download.DownloadError(f"cannot reach {url}: refused")
        exc.__cause__ = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        return exc
    if kind == "truncated":
        return http.client.IncompleteRead(b"half", 10)
    if kind == "truncated_wrapped":
        exc = download.DownloadError(f"cannot reach {url}: IncompleteRead")
        exc.__cause__ = http.client.IncompleteRead(b"half", 10)
        return exc
    if kind == "cert":
        exc = download.DownloadError(f"cannot reach {url}: certificate verify failed")
        exc.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed")
        return exc
    if kind == "size_cap":
        return download.DownloadError(f"response too large (over 64 bytes) for {url}")
    status = int(kind.split()[1])
    return download.HTTPStatusError(
        f"HTTP {status} for {url}",
        status=status,
        retry_after=download.parse_retry_after(shape),
        url=url,
    )


class Log:
    def __init__(self) -> None:
        self.lines: list[str] = []
        outer = self

        class Handler(logging.Handler):
            def emit(self, record):
                outer.lines.append(record.getMessage())

        self.handler = Handler()
        self.logger = logging.getLogger("nadare.core.sources")

    def __enter__(self):
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)


class WorldRun:
    """One execution of a world, with its observations for the coverage check."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.waits: list[tuple[float, Cancel | None]] = []
        self.clock = 0.0
        self.logs: list[str] = []


def run_world(world: World) -> WorldRun:
    run = WorldRun()
    token = Cancel()
    other = Cancel()
    if world.cancel_source == "arg_over_context":
        other.cancel()

    def transport(url, **kwargs):
        attempt = len(run.calls) + 1
        run.calls.append(url)
        if world.cancel_when == "call" and world.cancel_at == attempt:
            token.cancel()
        kind = world.script.get(attempt)
        if kind is None:
            return BODY
        raise _fault(kind, url, world.retry_after)

    def wait(seconds, cancel):
        run.waits.append((seconds, cancel))
        if world.cancel_when == "wait" and world.cancel_at == len(run.waits):
            token.cancel()
        if cancel is not None and cancel.cancelled:
            raise Cancelled()
        run.clock += seconds

    real_get, real_wait = download.http_get, download.wait
    real_retries = download.DEFAULT_RETRIES
    download.http_get = transport
    download.wait = wait
    download.DEFAULT_RETRIES = world.retries
    if world.cancel_when == "before":
        token.cancel()
    try:
        with Log() as log:
            try:
                if world.cancel_source == "context":
                    with sources.fetch_cancel(token):
                        data = sources.default_fetch(URL)
                elif world.cancel_source == "arg_over_context":
                    with sources.fetch_cancel(other):
                        data = sources.default_fetch(URL, cancel=token)
                elif world.cancel_source == "none":
                    data = sources.default_fetch(URL)
                else:
                    data = sources.default_fetch(URL, cancel=token)
                outcome, error = "ok", data
            except Cancelled as exc:
                outcome, error = "cancelled", exc
            except directdl.DirectError as exc:
                outcome, error = "error", exc
        run.logs = [line for line in log.lines if "fetch retry" in line]
    finally:
        download.http_get, download.wait = real_get, real_wait
        download.DEFAULT_RETRIES = real_retries

    calls, waits, want = expectations(world)
    assert outcome == want, (
        f"seed {world.seed}: outcome {outcome} != {want} "
        f"(script={world.script}, retries={world.retries}, "
        f"cancel={world.cancel_when}/{world.cancel_at}, error={error!r})"
    )
    assert len(run.calls) == calls, f"seed {world.seed}: {len(run.calls)} calls != {calls}"
    assert len(run.waits) == waits, f"seed {world.seed}: {len(run.waits)} waits != {waits}"
    if sources._fetch_cancel.get() is not None:
        raise AssertionError(f"seed {world.seed}: the fetch contextvar leaked")
    expected_token = None if world.cancel_source == "none" else token
    for seconds, seen in run.waits:
        assert seen is expected_token, f"seed {world.seed}: wait got {seen!r} as its token"
        assert math.isfinite(seconds), f"seed {world.seed}: non-finite wait {seconds!r}"
        assert 0.0 <= seconds <= MAX_WAIT, f"seed {world.seed}: wait {seconds!r} over the cap"
    for index, (seconds, _seen) in enumerate(run.waits, start=1):
        kind = world.script.get(index, "ok")
        exact = expected_wait(world.retry_after, index) if kind.startswith("http") else None
        if exact is not None:
            assert seconds == pytest.approx(exact), (
                f"seed {world.seed}: wait {seconds!r} != Retry-After {exact!r}"
            )
        else:
            base = BACKOFF[min(index - 1, len(BACKOFF) - 1)]
            assert base - 1e-9 <= seconds <= base * 1.2 + 1e-9, (
                f"seed {world.seed}: wait {seconds!r} is not the schedule around {base}"
            )
    assert len(run.logs) == len(run.waits), (
        f"seed {world.seed}: {run.logs!r} for {len(run.waits)} retried attempts"
    )
    prefix = "fetch retry"
    for line, (_seconds, _seen) in zip(run.logs, run.waits, strict=True):
        assert line.startswith(prefix), line
        assert "TOPSECRET" not in line and "?" not in line and "key=" not in line, line
        assert HOST in line, line
        reason = line.rsplit(": ", 1)[-1]
        assert reason in REASONS or reason.startswith("HTTP "), line
    if outcome == "ok":
        assert error == BODY
    if outcome == "error":
        text = str(error)
        assert "TOPSECRET" not in text and "?" not in text and "key=" not in text, text
        assert SAFE in text, text
    return run


# --------------------------------------------------------------------------- the tests
def test_smoke_20_worlds_always_run():
    """A fast, always-on sample of the matrix (no NADARE_SIM needed)."""
    seen = set()
    for seed in range(20):
        world = make_world(seed)
        run_world(world)
        seen.add(world.cancel_when)
    assert seen


@pytest.mark.skipif(os.environ.get("NADARE_SIM") != "1", reason="set NADARE_SIM=1")
def test_the_thousand_world_run(capsys):
    stats: dict[str, dict[str, int]] = {
        "faults": {},
        "outcomes": {},
        "cancel": {},
        "retry_after": {},
    }
    for seed in range(SEEDS):
        world = make_world(seed)
        run_world(world)
        for kind in [*world.script.values(), "ok"]:
            stats["faults"][kind] = stats["faults"].get(kind, 0) + 1
        _, _, outcome = expectations(world)
        stats["outcomes"][outcome] = stats["outcomes"].get(outcome, 0) + 1
        mode = f"{world.cancel_when}/{world.cancel_source}"
        stats["cancel"][mode] = stats["cancel"].get(mode, 0) + 1
        shape = "none" if world.retry_after is None else world.retry_after
        stats["retry_after"][shape] = stats["retry_after"].get(shape, 0) + 1
    with capsys.disabled():
        print(f"\n{SEEDS} worlds")
        for name, table in stats.items():
            print(f"{name}: {dict(sorted(table.items()))}")
    # The generator must keep covering the matrix, or the run would pass vacuously.
    assert set(stats["faults"]) == set(FAULTS)
    assert {"ok", "error", "cancelled"} <= set(stats["outcomes"])
    for mode in ("before", "call", "wait"):
        assert any(key.startswith(mode + "/") for key in stats["cancel"])
    for source in ("arg", "arg_over_context", "context", "none"):
        assert any(key.endswith("/" + source) for key in stats["cancel"])
    assert len(stats["retry_after"]) >= 10


@pytest.mark.skipif(os.environ.get("NADARE_SIM") != "1", reason="set NADARE_SIM=1")
def test_single_seed_replay():
    seed = int(os.environ.get("SIM_SEED", "0"))
    run_world(make_world(seed))


# --------------------------------------------------------------------------- focused
def test_retry_after_nan_is_a_finite_capped_wait(monkeypatch):
    """F1 of the simulation report: ``Retry-After: NaN`` must not skip the backoff."""
    monkeypatch.setattr("random.random", lambda: 0.0)
    calls, waits = [], []

    def get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise download.HTTPStatusError(
                f"HTTP 503 for {url}",
                status=503,
                retry_after=download.parse_retry_after("NaN"),
                url=url,
            )
        return BODY

    monkeypatch.setattr(download, "http_get", get)
    monkeypatch.setattr(download, "wait", lambda seconds, cancel: waits.append(seconds))
    assert sources.default_fetch(URL) == BODY
    assert waits == [2.0] and math.isfinite(waits[0])


def test_stop_on_the_final_failed_attempt_is_cancelled(monkeypatch):
    """F2 of the simulation report: retries exhausted by a Stop is not a network error."""
    calls, waits = [], []
    token = Cancel()

    def get(url, **kwargs):
        calls.append(url)
        if len(calls) == 3:
            token.cancel()
        exc = download.DownloadError(f"cannot reach {url}: timed out")
        exc.__cause__ = urllib.error.URLError(TimeoutError("timed out"))
        raise exc

    monkeypatch.setattr(download, "http_get", get)
    monkeypatch.setattr(download, "wait", lambda seconds, cancel: waits.append(seconds))
    monkeypatch.setattr(download, "DEFAULT_RETRIES", 3)
    with pytest.raises(Cancelled):
        sources.default_fetch(URL, cancel=token)
    assert len(calls) == 3 and len(waits) == 2


def test_the_token_a_backend_set_is_used_and_reset_after_an_error(monkeypatch):
    token = Cancel()

    def get(url, **kwargs):
        raise download.DownloadError(f"cannot reach {url}: timed out")

    monkeypatch.setattr(download, "http_get", get)
    monkeypatch.setattr(download, "wait", lambda seconds, cancel: None)
    monkeypatch.setattr(download, "DEFAULT_RETRIES", 1)
    with sources.fetch_cancel(token):
        with pytest.raises(directdl.DirectError):
            sources.default_fetch(URL)
    assert sources._fetch_cancel.get() is None


def test_no_world_opens_a_socket(monkeypatch):
    def refused(*_a, **_k):
        raise AssertionError("a world must not touch the network")

    monkeypatch.setattr(socket, "socket", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    for seed in range(20):
        run_world(make_world(seed))


def test_a_cancelled_token_in_this_thread_does_not_stop_a_worker_thread(monkeypatch):
    import threading

    token = Cancel()
    token.cancel()
    seen: dict[str, object] = {}

    def fetch():
        try:
            sources.default_fetch(URL)
        except BaseException as exc:
            seen["exc"] = exc
        else:
            seen["outcome"] = "ok"

    def get(url, **kwargs):
        raise download.DownloadError(f"cannot reach {url}: timed out")

    monkeypatch.setattr(download, "http_get", get)
    monkeypatch.setattr(download, "wait", lambda seconds, cancel: None)
    monkeypatch.setattr(download, "DEFAULT_RETRIES", 1)
    with sources.fetch_cancel(token):
        worker = threading.Thread(target=fetch)
        worker.start()
        worker.join()
    assert isinstance(seen.get("exc"), directdl.DirectError), f"worker got {seen!r}"
