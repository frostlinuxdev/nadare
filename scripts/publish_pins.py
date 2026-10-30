#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Publish block pins for direct-download games into ``<index>/catalog/archive-pins``.

Run by hand or from cron on ONE node; the file lock keeps two runs from writing
the same game at once. Every pin is only published after the streamed bytes
matched the publisher's md5, and a failed game leaves the old pin untouched::

    python scripts/publish_pins.py --index ../nadare-index --game aethergazer

Exit status: 0 = published, 3 = nothing to publish, anything else = error. A game
whose lock is held is skipped, not counted as a failure.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
_SRC = str(REPO / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from nadare.core import archivepins  # noqa: E402

UNCHANGED = 3
DEFAULT_MAX_BYTES = 128 << 30
DEFAULT_DEADLINE = 2 * 3600
_READ_CHUNK = 1 << 20
_TAG_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


class PublishPinError(RuntimeError):
    """A game cannot be pinned, or its pin cannot be published."""


class LockHeld(PublishPinError):
    """Another run holds the per-game lock; this run must skip the game."""


def _validate_tag(tag: str) -> None:
    if not tag or tag.startswith(".") or any(char not in _TAG_CHARS for char in tag):
        raise PublishPinError(f"invalid game tag {tag!r}")


def _check_block_size(block_size: int) -> None:
    try:
        archivepins.check_block_size(block_size)
    except archivepins.PinError as exc:
        raise PublishPinError(str(exc)) from exc


def resolver_for(tag: str) -> Callable[[], Any]:
    """The resolver that produces the latest plan for ``tag``."""
    from nadare.core.sources import aethergazer, mongil

    resolvers = {"aethergazer": aethergazer.resolve, "mongil": mongil.resolve}
    resolver = resolvers.get(tag)
    if resolver is None:
        raise PublishPinError(f"unknown game {tag!r}")
    return resolver


@contextmanager
def game_lock(index: Path, tag: str) -> Iterator[None]:
    """Hold a non-blocking per-game lock for the whole publish run."""
    _validate_tag(tag)
    lock_dir = index / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"archive-pins-{tag}.lock"
    handle = open(path, "a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockHeld(f"lock is held for {tag}") from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


@contextmanager
def _deadline(seconds: float | None) -> Iterator[Callable[[], None]]:
    """Abort the run with ``PublishPinError`` once ``seconds`` have passed."""
    if seconds is None or seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield lambda: None
        return

    def _expired(signum: int, frame: Any) -> None:
        raise PublishPinError(f"deadline of {seconds:g} seconds exceeded")

    previous = signal.getsignal(signal.SIGALRM)
    remaining, interval = signal.setitimer(signal.ITIMER_REAL, seconds)
    started = time.monotonic()
    armed = True

    def disarm() -> None:
        nonlocal armed
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            armed = False

    signal.signal(signal.SIGALRM, _expired)
    try:
        yield disarm
    finally:
        disarm()
        signal.signal(signal.SIGALRM, previous)
        if remaining > 0:
            left = remaining - (time.monotonic() - started)
            signal.setitimer(signal.ITIMER_REAL, max(0.001, left), interval)


def open_stream(url: str, headers: Mapping[str, str]) -> Iterator[bytes]:
    """Yield the bytes at ``url`` in 1 MiB chunks (the default network reader)."""
    from nadare.core import download

    pool = download.HttpClientPool()
    try:
        response = pool.open(url, headers=headers)
        try:
            if response.status != 200:
                raise PublishPinError(f"HTTP {response.status} for {url}")
            while True:
                chunk = response.read(_READ_CHUNK)
                if not chunk:
                    return
                yield chunk
        finally:
            response.close()
    finally:
        pool.close()


def _pin_path(index: Path, tag: str) -> Path:
    return index / "catalog" / "archive-pins" / f"{tag}.json"


def _read_pins(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise PublishPinError(f"cannot read {path}: {exc}") from exc
    try:
        return archivepins.parse_document(json.loads(raw.decode("utf-8")))
    except (ValueError, RecursionError, archivepins.PinError):
        print("damaged pin document, regenerating", file=sys.stderr)
        return {}


def _ensure_writable(path: Path) -> None:
    """Make sure the pin directory exists and accepts a temp file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        os.close(fd)
        os.unlink(tmp)
    except OSError as exc:
        raise PublishPinError(f"pin directory is not writable: {path.parent}") from exc


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a same-directory temp file and ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _validate_plan(plan: Any) -> None:
    """Refuse a plan the client could not use, before any byte is downloaded."""
    if not plan.archives:
        raise PublishPinError(f"plan for {plan.game_tag} has no archives")
    for archive in plan.archives:
        source = archive.source
        if archive.parts:
            raise PublishPinError(f"multi-part archives are not supported: {archive.name}")
        if not isinstance(source.md5, str) or not source.md5:
            raise PublishPinError(f"no publisher md5 for {archive.name}")
        if not isinstance(source.url, str):
            raise PublishPinError(f"url for {archive.name} is not a string")
        if not archivepins.is_safe_pin_url(source.url):
            raise PublishPinError(f"unsafe url for {archive.name}: {source.url}")


def publish_pin(
    index: Path,
    tag: str,
    *,
    resolve: Callable[[], Any] | None = None,
    open_stream: Callable[[str, Mapping[str, str]], Iterator[bytes]] = open_stream,
    block_size: int = archivepins.PIN_BLOCK,
    deadline: float | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Publish or refresh the pin document for ``tag``; return ``published`` or ``unchanged``."""
    from nadare.core import pinbuild

    _validate_tag(tag)
    _check_block_size(block_size)
    path = _pin_path(index, tag)
    with game_lock(index, tag), _deadline(deadline) as disarm:
        plan = (resolve or resolver_for(tag))()
        _validate_plan(plan)
        existing = _read_pins(path)
        wanted: dict[str, Any] = {}
        probed = False
        for archive in plan.archives:
            source = archive.source
            pinned = existing.get(archive.name)
            if (
                pinned is not None
                and pinned.matches([source], source.md5)
                and pinned.block_size == block_size
                and pinned.publisher_md5 is not None
                and pinned.publisher_md5 == source.md5.lower()
            ):
                wanted[archive.name] = pinned
                continue
            if not probed:
                _ensure_writable(path)
                probed = True
            try:
                pin = pinbuild.build_pin(
                    source.url,
                    open_stream(source.url, dict(source.headers)),
                    block_size=block_size,
                    size=source.size,
                    publisher_md5=source.md5,
                    max_bytes=max_bytes,
                )
            except pinbuild.PinBuildError as exc:
                raise PublishPinError(str(exc)) from exc
            wanted[archive.name] = pin
        doc = archivepins.document(wanted)
        data = (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > archivepins.MAX_DOCUMENT_BYTES:
            raise PublishPinError("pin document is too large to publish")
        try:
            archivepins.parse_document(json.loads(data))
        except archivepins.PinError as exc:
            raise PublishPinError(f"built pin document does not validate: {exc}") from exc
        if path.exists():
            try:
                current = path.read_bytes()
            except OSError:
                current = None
            if current == data:
                return "unchanged"
        disarm()
        _atomic_write_bytes(path, data)
        return "published"


def main(
    argv: list[str] | None = None,
    *,
    resolve: Callable[[], Any] | None = None,
    open_stream: Callable[[str, Mapping[str, str]], Iterator[bytes]] = open_stream,
) -> int:
    """Publish each requested game and report one line per game."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--game", required=True, action="append", dest="games")
    parser.add_argument("--block-size", type=int, default=archivepins.PIN_BLOCK)
    parser.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)
    failed = False
    published = False
    start = time.monotonic()
    for tag in args.games:
        budget = None
        if args.deadline > 0:
            budget = args.deadline - (time.monotonic() - start)
            if budget <= 0:
                print(f"{tag}: FAILED: deadline of {args.deadline:g} seconds exceeded")
                failed = True
                continue
        try:
            result = publish_pin(
                args.index,
                tag,
                resolve=resolve,
                open_stream=open_stream,
                block_size=args.block_size,
                deadline=budget,
                max_bytes=args.max_bytes,
            )
        except LockHeld:
            print(f"{tag}: skipped (lock is held)")
        except Exception as exc:  # noqa: BLE001 - one bad game must not stop the rest
            print(f"{tag}: FAILED: {exc}")
            failed = True
        else:
            print(f"{tag}: {result}")
            published = published or result == "published"
    if failed:
        return 1
    return 0 if published else UNCHANGED


if __name__ == "__main__":
    raise SystemExit(main())
