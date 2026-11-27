# SPDX-License-Identifier: GPL-3.0-or-later
"""Seeded world simulation of the pack pipeline (#1817).

A *world* is one synthetic install: 1-150 packs of random kind (zip, split zip, slices),
random member trees with paths that collide across packs, a random starting state (empty,
one pack already cached, one pack already installed, a stale half-tree), and a random set
of injected faults (network error, short read, corrupt bytes, hash mismatch, unpack error,
ENOSPC, cancel, crash). Every world runs the real :func:`nadare.core.directdl.sync` against
an in-process fake transport; nothing touches the network, the desktop or a real game.

The heavy run is off by default (NADARE_SIM=1); a 20-seed smoke test always runs.
"""

from __future__ import annotations

import builtins
import errno
import hashlib
import io
import os
import random
import shutil
import threading
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nadare.core import directdl as dd
from nadare.core import download as dlmod
from nadare.core import transfer
from nadare.core.events import Cancel, Cancelled

SIM_ENABLED = os.environ.get("NADARE_SIM") == "1"
DEFAULT_SEEDS = int(os.environ.get("SIM_SEEDS") or (1000 if SIM_ENABLED else 20))
SEED_BASE = int(os.environ.get("SIM_SEED_BASE") or 0)
ONLY_SEED = os.environ.get("SIM_ONLY_SEED")

#: The design bound from directdl (#1817): two packs on disk at once.
DESIGN_PACKS = 2
GAME = "simpack"
VERSION = "2.0"

FAULT_KINDS = (
    "download_error",
    "short_read",
    "corrupt",
    "hash_mismatch",
    "unpack_error",
    "disk_full",
    "cancel",
    "crash",
)

SHARED_PATHS = (
    "common/engine.bin",
    "common/ui.bin",
    "data/shared.dat",
    "cfg/game.ini",
)
STOP_KINDS = frozenset({"cancel", "crash"})

#: Invariants this build is known to break (reported as product bugs); the always-on
#: smoke test lets exactly these through so a real regression still stands out.
KNOWN_PRODUCT_BUGS: frozenset[str] = frozenset()  # F1/F2 fixed (fix/1126-packsec)

#: Exceptions a sync is allowed to raise in a world; anything else is a simulator finding.
EXPECTED_ERRORS = (Cancelled, dd.DirectError, OSError, dlmod.DownloadError)


class SimCrash(BaseException):
    """A process crash raised out of the transport or an unpack hook."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, body in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            zf.writestr(info, body)
    return buffer.getvalue()


def _corrupt_zip_same_size(files: dict[str, bytes]) -> bytes:
    """A valid zip: same names, same sizes, different bytes, same length."""
    flipped = {name: bytes(value ^ 0xFF for value in body) for name, body in files.items()}
    return _make_zip(flipped)


def _break_zip(blob: bytes) -> bytes:
    """The same blob with its central directory zeroed: zipfile refuses it, size unchanged."""
    data = bytearray(blob)
    start = data.rfind(b"PK\x01\x02")
    if start < 0:  # pragma: no cover - our zips always have a central directory
        start = max(0, len(data) - 64)
    data[start : start + 32] = b"\x00" * 32
    return bytes(data)


def _flip(blob: bytes, rng: random.Random) -> bytes:
    data = bytearray(blob)
    data[rng.randrange(len(data))] ^= 0xFF
    return bytes(data)


@dataclass
class _Pack:
    index: int
    name: str
    kind: str  # "zip" | "parts" | "slices"
    members: dict[str, bytes]
    archive: dd.Archive
    requests: dict[str, bytes]  # URL -> the correct body of that request
    request_paths: dict[str, str]  # URL -> cache-relative path under .nadare-dl
    hashes: str  # "md5" | "sha256" | "none"


@dataclass
class _Fault:
    kind: str
    pack: int | None = None
    url: str | None = None
    body: bytes | None = None  # corrupt body replacement
    at: int = 0  # byte offset for transport faults / stop moments
    attempts: int = 1
    moment: str = ""  # "download" | "unpack" | "between"
    reads: int = 0  # unpack stop: act after this many staging reads
    budget: int = 0  # disk_full byte budget
    seen: int = 0
    fired: bool = False

    def describe(self) -> str:
        where = f" pack={self.pack}" if self.pack is not None else ""
        if self.moment == "download":
            where += f" at={self.at}"
        return f"{self.kind}{where} attempts={self.attempts}"


@dataclass
class _World:
    seed: int
    packs: list[_Pack]
    plan: dd.Plan
    expected: dict[str, bytes]
    variant: str
    cached_pack: int | None = None
    done_pack: int | None = None
    stale: dict[str, bytes] = field(default_factory=dict)
    faults: list[_Fault] = field(default_factory=list)

    def names(self) -> dict[str, int]:
        """cache-relative name -> pack index, for the on-disk sampler."""
        names: dict[str, int] = {}
        for pack in self.packs:
            for relative in pack.request_paths.values():
                names[relative] = pack.index
        return names

    def describe(self) -> str:
        faults = ", ".join(fault.describe() for fault in self.faults) or "none"
        return f"seed={self.seed} packs={len(self.packs)} variant={self.variant} faults=[{faults}]"


def _member_bytes(rng: random.Random) -> bytes:
    return bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 512)))


def _with_hash(rf: dd.RemoteFile, blob: bytes, hashes: str) -> dd.RemoteFile:
    if hashes == "md5":
        return dd.RemoteFile(rf.path, rf.url, rf.size, md5=_md5(blob), headers=rf.headers)
    if hashes == "sha256":
        return dd.RemoteFile(
            rf.path, rf.url, rf.size, sha256=hashlib.sha256(blob).hexdigest(), headers=rf.headers
        )
    return rf


def _build_pack(rng: random.Random, index: int) -> _Pack:
    kind = rng.choices(("zip", "parts", "slices"), (0.6, 0.2, 0.2))[0]
    members: dict[str, bytes] = {}
    for slot in range(rng.randint(1, 3)):
        if rng.random() < 0.35:
            path = rng.choice(SHARED_PATHS)
        else:
            path = f"pack{index:03d}/f{slot}.bin"
        members[path] = _member_bytes(rng)
    hashes = rng.choices(("md5", "sha256", "none"), (0.55, 0.2, 0.25))[0]
    name = f"pack-{index:03d}"
    base = f"p{index:03d}"

    if kind == "zip":
        blob = _make_zip(members)
        source = _with_hash(
            dd.RemoteFile(f"{base}.zip", f"https://sim.invalid/{base}.zip", len(blob)),
            blob,
            hashes,
        )
        archive = dd.Archive(name, source, unwrap=("zip",))
        requests = {source.url: blob}
        paths = {source.url: source.path}
    elif kind == "parts":
        blob = _make_zip(members)
        cuts = sorted(rng.sample(range(1, len(blob)), rng.randint(1, 2)))
        bounds = zip([0, *cuts], [*cuts, len(blob)], strict=False)
        pieces = [blob[start:end] for start, end in bounds]
        parts = tuple(
            dd.RemoteFile(f"{base}-{i}.bin", f"https://sim.invalid/{base}-{i}.bin", len(piece))
            for i, piece in enumerate(pieces)
        )
        source = _with_hash(
            dd.RemoteFile(f"{base}.zip", f"https://sim.invalid/{base}.zip", len(blob)),
            blob,
            hashes,
        )
        archive = dd.Archive(name, source, parts=parts, unwrap=("zip",))
        requests = {part.url: piece for part, piece in zip(parts, pieces, strict=True)}
        paths = {part.url: part.path for part in parts}
    else:
        entries: list[dd.Member] = []
        chunks: list[bytes] = []
        offset = 0
        for path, data in members.items():
            pad = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 8)))
            chunks.append(pad)
            offset += len(pad)
            md5 = _md5(data) if rng.random() < 0.5 else None
            entries.append(dd.Member(path, offset, len(data), md5=md5))
            chunks.append(data)
            offset += len(data)
        blob = b"".join(chunks)
        source = _with_hash(
            dd.RemoteFile(f"{base}.bin", f"https://sim.invalid/{base}.bin", len(blob)),
            blob,
            hashes,
        )
        archive = dd.Archive(name, source, unwrap=("slices",), members=tuple(entries))
        requests = {source.url: blob}
        paths = {source.url: source.path}

    return _Pack(index, name, kind, members, archive, requests, paths, hashes)


def _build_world(seed: int) -> _World:
    rng = random.Random(seed)
    count = rng.randint(1, 150)
    packs = [_build_pack(rng, index) for index in range(count)]
    expected: dict[str, bytes] = {}
    for pack in packs:
        expected.update(pack.members)
    plan = dd.Plan(GAME, VERSION, archives=tuple(pack.archive for pack in packs))

    variant = rng.choices(("empty", "cached", "done", "stale"), (0.4, 0.2, 0.2, 0.2))[0]
    world = _World(seed, packs, plan, expected, variant)

    if variant == "cached":
        # One archive already sits in .nadare-dl (a crash between download and unpack).
        # Index 0 or 1: the pipeline unpacks it first, so the on-disk bound stays meaningful.
        world.cached_pack = rng.choice([0, 1] if count > 1 else [0])
    elif variant == "done":
        counts = Counter(path for pack in packs for path in pack.members)
        unique = [
            pack.index
            for pack in packs
            if pack.members and all(counts[path] == 1 for path in pack.members)
        ]
        if unique:
            world.done_pack = rng.choice(unique)
        else:
            world.variant = "cached"
            world.cached_pack = rng.choice([0, 1] if count > 1 else [0])
    elif variant == "stale":
        paths = list(expected)
        rng.shuffle(paths)
        for path in paths[: rng.randint(1, min(10, len(paths)))]:
            # Half the stale files get identical bytes: the tree may not change at all.
            world.stale[path] = expected[path] if rng.random() < 0.5 else _member_bytes(rng)

    _plan_faults(rng, world)
    return world


def _requestable(world: _World) -> list[_Pack]:
    blocked = {world.cached_pack, world.done_pack}
    return [pack for pack in world.packs if pack.index not in blocked]


def _plan_faults(rng: random.Random, world: _World) -> None:
    count = rng.choices((0, 1, 2, 3), (0.3, 0.35, 0.22, 0.13))[0]
    requestable = _requestable(world)
    if count == 0 or not requestable:
        return
    candidates: list[str] = []
    for kind in FAULT_KINDS:
        if kind in ("download_error", "short_read", "cancel", "crash", "disk_full"):
            candidates.append(kind)
        elif kind == "corrupt":
            if any(pack.hashes != "none" for pack in requestable):
                candidates.append(kind)
        elif kind == "hash_mismatch":
            if any(pack.hashes == "sha256" for pack in requestable):
                candidates.append(kind)
        elif kind == "unpack_error":
            if any(pack.hashes == "none" and pack.kind in ("zip", "parts") for pack in requestable):
                candidates.append(kind)
    rng.shuffle(candidates)
    chosen = candidates[:count]

    # Keep at most one fault that ends the first sync; the others are made one-shot so they
    # heal inside the same sync and the world stays a resume test rather than a pile-up.
    multi = len(chosen) > 1
    if multi:
        kept: list[str] = []
        has_stopper = False
        for kind in chosen:
            stopper = kind in ("cancel", "crash", "disk_full", "unpack_error", "corrupt")
            if stopper and has_stopper:
                continue
            has_stopper = has_stopper or stopper
            kept.append(kind)
        chosen = kept

    # All pack faults land near one focus pack, so several of them actually get reached
    # before whichever one stops the sync.
    focus = rng.choice(requestable)
    near = [pack for pack in requestable if abs(pack.index - focus.index) <= 1]

    def pick(predicate) -> _Pack:
        pool = [pack for pack in near if predicate(pack)]
        if not pool:
            pool = [pack for pack in requestable if predicate(pack)]
        return rng.choice(pool)

    for kind in chosen:
        attempts = rng.choice((1, 1, 3)) if not multi else 1
        if kind == "disk_full":
            world.faults.append(_Fault(kind, budget=rng.randint(1, 4096), moment="write"))
            continue
        if kind == "corrupt":
            pack = pick(lambda p: p.hashes != "none")
            url = rng.choice(sorted(pack.requests))
            if multi:
                attempts = 1
            world.faults.append(
                _Fault(
                    kind,
                    pack.index,
                    url,
                    body=_corrupt_body(pack, url, rng),
                    attempts=attempts,
                )
            )
            continue
        if kind == "hash_mismatch":
            pack = pick(lambda p: p.hashes == "sha256")
            url = rng.choice(sorted(pack.requests))
            world.faults.append(
                _Fault(
                    kind,
                    pack.index,
                    url,
                    body=_corrupt_body(pack, url, rng),
                    attempts=attempts,
                )
            )
            continue
        if kind == "unpack_error":
            pack = pick(lambda p: p.hashes == "none" and p.kind in ("zip", "parts"))
            url = rng.choice(sorted(pack.requests))
            world.faults.append(
                _Fault(
                    kind,
                    pack.index,
                    url,
                    body=_break_zip(pack.requests[url]),
                    attempts=attempts,
                )
            )
            continue

        pack = pick(lambda p: True)
        url = rng.choice(sorted(pack.requests))
        size = len(pack.requests[url])
        if kind in ("download_error", "short_read"):
            at = rng.randint(max(1, size // 4), max(1, size - 1))
            world.faults.append(_Fault(kind, pack.index, url, at=at, attempts=attempts))
        else:  # cancel / crash
            moment = rng.choices(("download", "unpack", "between"), (0.4, 0.4, 0.2))[0]
            if moment == "download":
                world.faults.append(
                    _Fault(
                        kind,
                        pack.index,
                        url,
                        at=rng.randint(0, max(0, size - 1)),
                        moment="download",
                    )
                )
            else:
                unpackable = [p for p in near if p.index != world.done_pack]
                if not unpackable:
                    unpackable = [p for p in world.packs if p.index != world.done_pack]
                target = rng.choice(unpackable)
                if target.kind == "slices":
                    moment = "between"  # slices reads are not wrapped by _CancelReader
                world.faults.append(
                    _Fault(kind, target.index, moment=moment, reads=rng.randint(0, 3))
                )


def _corrupt_body(pack: _Pack, url: str, rng: random.Random) -> bytes:
    body = pack.requests[url]
    if pack.kind == "zip":
        # A still-valid zip with different bytes; rebuilding a split archive would change
        # the individual part lengths.
        return _corrupt_zip_same_size(pack.members)
    return _flip(body, rng)


class _Response:
    """Just enough of a urllib response for ``download._download_once``."""

    def __init__(
        self, net: _FakeNet, body: bytes, offset: int, action: str | None, fault: _Fault | None
    ) -> None:
        self._net = net
        self._body = body
        self._pos = offset
        self._action = action
        self._fault = fault
        self._acted = False
        self.status = 200 if offset == 0 else 206
        self.headers = {"Content-Length": str(len(body) - offset)}
        if offset:
            self.headers["Content-Range"] = f"bytes {offset}-{len(body) - 1}/{len(body)}"

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def close(self) -> None:
        pass

    def read(self, amount: int = -1) -> bytes:
        if self._pos >= len(self._body):
            return b""
        # Small chunks are normal socket reads; they let a fault land inside a body.
        size = 13 if amount is None or amount < 0 else min(amount, 13)
        stop = self._pos + size
        due = (
            self._action is not None
            and not self._acted
            and self._fault is not None
            and self._fault.at < stop
        )
        if due:
            stop = max(self._pos, min(self._fault.at, stop))  # type: ignore[union-attr]
        chunk = self._body[self._pos : stop]
        self._pos += len(chunk)
        self._net.served += len(chunk)
        self._net.on_read()
        if due:
            self._acted = True
            if self._fault is not None:
                self._fault.fired = True
            if self._action == "short":
                # EOF before the declared length; a closed connection stays closed, so
                # later reads must not resume past the dropped bytes.
                self._pos = len(self._body)
                return b""
            self._fire()
        return chunk

    def _fire(self) -> None:
        if self._action == "error":
            raise dlmod.DownloadError("simulated network error")
        if self._action == "cancel":
            self._net.token.cancel()
            return
        if self._action == "crash":
            raise SimCrash("simulated process crash while downloading")
        raise AssertionError(f"unknown transport action {self._action!r}")


class _FakeNet:
    """An in-process HTTP origin with per-URL faults. No sockets, no DNS."""

    def __init__(self) -> None:
        self.routes: dict[str, bytes] = {}
        self.faults: dict[str, _Fault] = {}
        self.served = 0
        self.round = -1  # faults only fire while a round is armed
        self.token = Cancel()
        self.requests: list[str] = []
        self.sampler: _PeakSampler | None = None
        self.url_pack: dict[str, int] = {}
        self.touched: set[int] = set()
        self._reads = 0

    def install(self, world: _World) -> None:
        self.routes = {}
        self.faults = {}
        self.url_pack = {}
        for pack in world.packs:
            for url, body in pack.requests.items():
                self.routes[url] = body
                self.url_pack[url] = pack.index
        for fault in world.faults:
            if fault.url is not None:
                self.faults[fault.url] = fault

    def reset_round(self) -> None:
        self.served = 0
        self.requests.clear()
        self.touched.clear()
        self._reads = 0
        self.token = Cancel()
        for fault in self.faults.values():
            fault.seen = 0

    def http_open(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        redirect_allowed: Any = None,
        data: Any = None,
    ) -> _Response:
        del timeout, redirect_allowed, data
        assert url in self.routes, f"unexpected request {url}"
        self.requests.append(url)
        self.touched.add(self.url_pack[url])
        if self.sampler is not None:
            self.sampler.sample()
        body = self.routes[url]
        action: str | None = None
        active: _Fault | None = None
        fault = self.faults.get(url)
        if fault is not None and self.round >= 0:
            fault.seen += 1
            if fault.seen <= fault.attempts:
                active = fault
                action = {
                    "download_error": "error",
                    "short_read": "short",
                    "cancel": "cancel",
                    "crash": "crash",
                }.get(fault.kind)
                if fault.body is not None:
                    body = fault.body
                if action is None:
                    fault.fired = True  # a corrupt body was served
        offset = 0
        range_header = (headers or {}).get("Range")
        if range_header and range_header.startswith("bytes="):
            offset = int(range_header.removeprefix("bytes=").split("-", 1)[0])
            offset = min(max(offset, 0), len(body))
        # The real http_open always wraps the body in transfer.Response, which meters the
        # bytes the sync reports; the fake transport must go through the same meter.
        return transfer.Response(  # type: ignore[return-value]
            _Response(self, body, offset, action, active if action else None), url
        )

    def on_read(self) -> None:
        self._reads += 1
        if self.sampler is not None and self._reads % 32 == 0:
            self.sampler.sample()


class _WriteBudget:
    """Wrap ``open`` so writes under the world root fail with ENOSPC after a budget."""

    def __init__(self, root: Path, budget: int) -> None:
        self._root = str(root.resolve())
        self._left = budget
        self.fired = False
        self._real_open = builtins.open

    def open(self, file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = self._real_open(file, mode, *args, **kwargs)
        if not any(flag in mode for flag in "wax+"):
            return handle
        try:
            path = str(Path(file).resolve())
        except (OSError, TypeError):
            return handle
        if not path.startswith(self._root + os.sep):
            return handle
        return _BudgetFile(handle, self)

    def spend(self, amount: int) -> None:
        if self.fired:
            raise OSError(errno.ENOSPC, "simulated disk full")
        self._left -= amount
        if self._left < 0:
            self.fired = True
            raise OSError(errno.ENOSPC, "simulated disk full")


class _BudgetFile:
    def __init__(self, handle: Any, budget: _WriteBudget) -> None:
        self._handle = handle
        self._budget = budget

    def write(self, data: Any) -> int:
        self._budget.spend(len(data))
        return self._handle.write(data)

    def __enter__(self) -> _BudgetFile:
        return self

    def __exit__(self, *exc: object) -> bool:
        self._handle.close()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


class _PeakSampler(threading.Thread):
    """Samples .nadare-dl while a sync runs: distinct packs on disk at once."""

    def __init__(
        self, dest: Path, names: dict[str, int], baseline: set[int], net: _FakeNet
    ) -> None:
        super().__init__(name="sim-peak-sampler", daemon=True)
        self._dl_dir = dest / dd.DL_DIR
        self._names = names
        self._baseline = baseline
        self._net = net
        self._halt = threading.Event()
        self.peak = 0
        self.witness: list[str] = []

    def run(self) -> None:
        while not self._halt.is_set():
            self.sample()
            self._halt.wait(0.001)

    def sample(self) -> None:
        try:
            entries = list(self._dl_dir.iterdir())
        except OSError:
            return
        present: set[int] = set()
        names: list[str] = []
        for entry in entries:
            name = entry.name
            if name.endswith(".part"):
                name = name[: -len(".part")]
            index = self._names.get(name)
            if index is not None:
                present.add(index)
                names.append(entry.name)
        # Files a crashed/stopped earlier run left behind wait their turn; they are not
        # in flight. A baseline pack this round requests again (a partial resume) counts.
        active = present - (self._baseline - self._net.touched)
        if len(active) > self.peak:
            self.peak = len(active)
            self.witness = sorted(names)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=5)


@dataclass
class _Failure:
    seed: int
    invariant: str
    detail: str
    world: _World
    known: bool = False


def _present_packs(dest: Path, names: dict[str, int]) -> set[int]:
    """Packs with a cache file already on disk when a round starts."""
    try:
        entries = list((dest / dd.DL_DIR).iterdir())
    except OSError:
        return set()
    present: set[int] = set()
    for entry in entries:
        name = entry.name[: -len(".part")] if entry.name.endswith(".part") else entry.name
        index = names.get(name)
        if index is not None:
            present.add(index)
    return present


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every installed file (state and cache folders left out) as path -> content."""
    files: dict[str, bytes] = {}
    if not root.is_dir():
        return files
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative.split("/", 1)[0] in (dd.STATE_DIR, dd.DL_DIR):
            continue
        if path.is_file():
            files[relative] = path.read_bytes()
    return files


def _tree_hash(files: dict[str, bytes]) -> dict[str, str]:
    return {path: _sha(data) for path, data in files.items()}


def _allowed_contents(world: _World, before: dict[str, bytes]) -> dict[str, set[str]]:
    allowed: dict[str, set[str]] = {}
    for path, data in before.items():
        allowed.setdefault(path, set()).add(_sha(data))
    for pack in world.packs:
        for path, data in pack.members.items():
            allowed.setdefault(path, set()).add(_sha(data))
    return allowed


def _check_stopped(
    world: _World,
    dest: Path,
    before: dict[str, bytes],
    changed: threading.Event,
    failures: list[_Failure],
    leaks: list[str],
) -> None:
    tree = _snapshot(dest)
    allowed = _allowed_contents(world, before)
    for path, data in tree.items():
        if path not in allowed:
            failures.append(
                _Failure(
                    world.seed,
                    "temp_file_left",
                    f"unexpected file outside staging: {path!r}",
                    world,
                )
            )
            continue
        if _sha(data) not in allowed[path]:
            failures.append(
                _Failure(
                    world.seed,
                    "half_written_file",
                    f"{path!r} holds {len(data)} bytes that are neither old nor complete",
                    world,
                )
            )
    if not changed.is_set() and _tree_hash(tree) != _tree_hash(before):
        failures.append(
            _Failure(
                world.seed,
                "changed_unset_but_tree_differs",
                "sync failed without setting changed, yet the tree differs",
                world,
            )
        )
    for name in leaks:
        failures.append(_Failure(world.seed, "thread_leaked", f"{name!r} still alive", world))


def _shared_member_regressions(world: _World, tree: dict[str, str]) -> list[str]:
    """Wrong paths whose bytes are another pack's copy of a path several packs ship.

    A later pack that was already installed is skipped on a resumed sync, while an earlier
    pack that failed is unpacked again and overwrites the shared member. The result is a
    complete file from an older pack, not a torn write, but it is not the plan's tree.
    """
    counts = Counter(path for pack in world.packs for path in pack.members)
    regressions: list[str] = []
    for path, digest in tree.items():
        if path not in world.expected or digest == _sha(world.expected[path]):
            continue
        if counts[path] < 2:
            continue
        copies = {
            _sha(data)
            for pack in world.packs
            for name, data in pack.members.items()
            if name == path
        }
        if digest in copies:
            regressions.append(path)
    return regressions


def _check_success(
    world: _World,
    dest: Path,
    summary: Any,
    served: int,
    before: dict[str, bytes],
    changed: threading.Event,
    peak: int,
    failures: list[_Failure],
    leaks: list[str],
    stats: Counter,
    resumed: bool = False,
    fresh_clean: bool = False,
) -> None:
    tree = _tree_hash(_snapshot(dest))
    expected = _tree_hash(world.expected)
    if tree != expected:
        wrong = [path for path in set(tree) & set(expected) if tree[path] != expected[path]]
        known_regressions = _shared_member_regressions(world, tree) if resumed else []
        hard_wrong = [path for path in wrong if path not in known_regressions]
        if known_regressions:
            stats["resume_shared_member_regression"] += 1
            failures.append(
                _Failure(
                    world.seed,
                    "resume_shared_member_regression",
                    f"shared members hold an earlier pack's copy: {known_regressions[:3]}",
                    world,
                    known="resume_shared_member_regression" in KNOWN_PRODUCT_BUGS,
                )
            )
        if hard_wrong or set(tree) != set(expected):
            missing = sorted(set(expected) - set(tree))[:3]
            extra = sorted(set(tree) - set(expected))[:3]
            failures.append(
                _Failure(
                    world.seed,
                    "tree_not_expected",
                    f"missing={missing} extra={extra} wrong={hard_wrong[:3]}",
                    world,
                )
            )
    if tree != _tree_hash(before) and not changed.is_set():
        failures.append(
            _Failure(
                world.seed,
                "changed_not_set_on_success",
                "the tree was written but changed was never set",
                world,
            )
        )
    if tree == _tree_hash(before) and changed.is_set():
        stats["changed_false_positive"] += 1
    if peak > DESIGN_PACKS:
        failures.append(
            _Failure(
                world.seed,
                "peak_packs_exceeded",
                f"{peak} packs on disk at once "
                f"({world.__dict__.get('_peak_witness')}), bound is {DESIGN_PACKS}",
                world,
            )
        )
    dl_dir = dest / dd.DL_DIR
    if dl_dir.exists():
        leftovers = sorted(entry.name for entry in dl_dir.iterdir())
        if leftovers:
            failures.append(
                _Failure(
                    world.seed,
                    "staging_not_cleaned",
                    f".nadare-dl still holds {leftovers[:5]}",
                    world,
                )
            )
    fetched = int(getattr(summary, "fetched_bytes", 0))
    if fresh_clean and fetched != served:
        failures.append(
            _Failure(
                world.seed,
                "fetched_bytes_mismatch",
                f"summary reports {fetched} bytes, the transport served {served}",
                world,
            )
        )
    elif fetched > served:
        stats["fetched_overcount"] += 1
        failures.append(
            _Failure(
                world.seed,
                "fetched_bytes_overcount",
                f"summary reports {fetched} bytes, the transport served {served}",
                world,
                known="fetched_bytes_overcount" in KNOWN_PRODUCT_BUGS,
            )
        )
    elif fetched < served:
        stats["retry_wire_overhead"] += served - fetched
    for name in leaks:
        failures.append(_Failure(world.seed, "thread_leaked", f"{name!r} still alive", world))


def _leaked(baseline: set[threading.Thread]) -> list[str]:
    deadline = time.monotonic() + 2.0
    while True:
        extra = [
            thread
            for thread in threading.enumerate()
            if thread not in baseline
            and thread.is_alive()
            and thread is not threading.current_thread()
        ]
        if not extra or time.monotonic() >= deadline:
            return [thread.name for thread in extra]
        time.sleep(0.02)


def _run_round(
    world: _World, dest: Path, net: _FakeNet, faults_on: bool, workers: int, stop: _Fault | None
) -> tuple[Any, threading.Event, int, list[str]]:
    net.install(world)
    net.round = 0 if faults_on else -1
    net.reset_round()
    token = net.token
    changed = threading.Event()

    real_unwrap = dd._unwrap
    real_read = dd._CancelReader.read
    state: dict[str, Any] = {"name": None, "reads": 0}

    def unwrap(archive, source, target, dl_dir, cancel=None, changed_event=None, **kw):
        state["name"] = archive.name
        state["reads"] = 0
        try:
            if stop is not None and archive.name == world.packs[stop.pack].name:
                if stop.moment == "between":
                    if stop.kind == "cancel" and cancel is not None:
                        cancel.cancel()
                    else:
                        stop.fired = True
                        raise SimCrash("simulated process crash before unpack")
            return real_unwrap(archive, source, target, dl_dir, cancel, changed_event, **kw)
        finally:
            state["name"] = None

    def read(self, size: int = -1) -> bytes:
        data = real_read(self, size)
        if (
            stop is not None
            and stop.moment == "unpack"
            and state["name"] == world.packs[stop.pack].name
        ):
            if state["reads"] == stop.reads:
                stop.fired = True
                if stop.kind == "cancel":
                    self._cancel.cancel()
                else:
                    raise SimCrash("simulated process crash during unpack")
            state["reads"] += 1
        return data

    hooks_installed = bool(stop)
    if hooks_installed:
        dd._unwrap = unwrap  # type: ignore[assignment]
        dd._CancelReader.read = read  # type: ignore[method-assign]

    budget: _WriteBudget | None = None
    for fault in world.faults:
        if fault.kind == "disk_full" and faults_on:
            budget = _WriteBudget(dest, fault.budget)
            builtins.open = budget.open  # type: ignore[assignment]
            break

    baseline_packs = _present_packs(dest, world.names())
    sampler = _PeakSampler(dest, world.names(), baseline_packs, net)
    net.sampler = sampler
    baseline = set(threading.enumerate())
    sampler.start()
    summary = None
    try:
        summary = dd.sync(world.plan, dest, workers=workers, cancel=token, changed=changed)
    except EXPECTED_ERRORS:
        pass
    except SimCrash:
        pass
    except Exception as exc:  # noqa: BLE001 - a simulator finding, not a world failure
        world.__dict__.setdefault("_unexpected", []).append(repr(exc))
    finally:
        sampler.stop()
        net.sampler = None
        if budget is not None:
            builtins.open = budget._real_open  # type: ignore[assignment]
            for fault in world.faults:
                if fault.kind == "disk_full":
                    fault.fired = budget.fired
        if hooks_installed:
            dd._unwrap = real_unwrap  # type: ignore[assignment]
            dd._CancelReader.read = real_read  # type: ignore[method-assign]
    leaks = _leaked(baseline)
    world.__dict__["_peak_witness"] = sampler.witness
    return summary, changed, sampler.peak, leaks


def _play(world: _World, root: Path, stats: Counter, failures: list[_Failure]) -> None:
    dest = root / "game"
    dest.mkdir(parents=True)
    rng = random.Random(world.seed ^ 0x5EED)
    workers = rng.randint(1, 4)
    net = _FakeNet()
    real_http_open = dlmod.http_open
    real_pool_can_serve = dlmod._pool_can_serve
    real_sleep = dlmod._sleep
    dlmod.http_open = net.http_open  # type: ignore[assignment]
    dlmod._pool_can_serve = lambda url: False  # type: ignore[assignment]
    dlmod._sleep = lambda seconds, cancel: None  # type: ignore[assignment]
    try:
        _prepare(world, dest, net, stats)
        before = _snapshot(dest)
        fresh_clean = not world.faults and not _present_packs(dest, world.names())
        summary, changed, peak, leaks = _run_round(
            world, dest, net, True, workers, _stop_fault(world)
        )
        stats["rounds"] += 1
        if summary is None:
            stats["failed_rounds"] += 1
            _check_stopped(world, dest, before, changed, failures, leaks)
            if any(fault.kind == "crash" and fault.fired for fault in world.faults):
                _plant_crash_residue(dest)
        else:
            stats["successful_rounds"] += 1
            stats["peak"] = max(stats["peak"], peak)
            _check_success(
                world,
                dest,
                summary,
                net.served,
                before,
                changed,
                peak,
                failures,
                leaks,
                stats,
                fresh_clean=fresh_clean,
            )
            return

        # A failure must leave a resumable folder: the second run has to finish.
        before_second = _snapshot(dest)
        summary, changed, peak, leaks = _run_round(world, dest, net, False, workers, None)
        stats["rounds"] += 1
        if summary is None:
            stats["failed_rounds"] += 1
            failures.append(
                _Failure(
                    world.seed,
                    "resume_failed",
                    "the second sync on the same folder failed too",
                    world,
                )
            )
            _check_stopped(world, dest, before_second, changed, failures, leaks)
            return
        stats["successful_rounds"] += 1
        stats["peak"] = max(stats["peak"], peak)
        _check_success(
            world,
            dest,
            summary,
            net.served,
            before_second,
            changed,
            peak,
            failures,
            leaks,
            stats,
            resumed=True,
        )
    finally:
        dlmod.http_open = real_http_open  # type: ignore[assignment]
        dlmod._pool_can_serve = real_pool_can_serve  # type: ignore[assignment]
        dlmod._sleep = real_sleep  # type: ignore[assignment]
        unexpected = world.__dict__.pop("_unexpected", [])
        for detail in unexpected:
            failures.append(_Failure(world.seed, "unexpected_exception", detail, world))


def _prepare(world: _World, dest: Path, net: _FakeNet, stats: Counter) -> None:
    net.round = -1
    if world.variant == "cached" and world.cached_pack is not None:
        pack = world.packs[world.cached_pack]
        for url, relative in pack.request_paths.items():
            target = dest / dd.DL_DIR / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(pack.requests[url])
        stats["variant_cached"] += 1
    elif world.variant == "done" and world.done_pack is not None:
        pack = world.packs[world.done_pack]
        net.install(world)
        dd.sync(dd.Plan(GAME, VERSION, archives=(pack.archive,)), dest, workers=1)
        stats["variant_done"] += 1
    elif world.variant == "stale":
        for path, data in world.stale.items():
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        stats["variant_stale"] += 1
    else:
        stats["variant_empty"] += 1


def _stop_fault(world: _World) -> _Fault | None:
    for fault in world.faults:
        if fault.kind in STOP_KINDS and fault.moment in ("unpack", "between"):
            return fault
    return None


def _plant_crash_residue(dest: Path) -> None:
    """What a kill -9 leaves that a clean exception unwind would not."""
    staging = dest / dd.DL_DIR / "extract-simcrash"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "half-extracted.bin").write_bytes(b"half")
    (dest / "stray.nadare-new.part").write_bytes(b"stray")
    (dest / "blob.nadare-gz").write_bytes(b"stray")


def _count_faults(world: _World, counter: Counter) -> None:
    for fault in world.faults:
        counter[fault.kind] += 1
        if fault.moment in ("download", "unpack", "between"):
            counter[f"{fault.kind}_{fault.moment}"] += 1
        if fault.fired:
            counter["fired"] += 1
            counter[f"{fault.kind}_fired"] += 1
        else:
            counter["not_fired"] += 1
            counter[f"{fault.kind}_not_fired"] += 1


def run_seeds(seeds: list[int]) -> tuple[list[_Failure], Counter, float]:
    stats: Counter = Counter()
    failures: list[_Failure] = []
    started = time.monotonic()
    scratch = Path(os.environ.get("SIM_SCRATCH") or "/tmp/opencode/sim-work")
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        for position, seed in enumerate(seeds, 1):
            world = _build_world(seed)
            root = scratch / f"seed-{seed}"
            try:
                _play(world, root, stats, failures)
            finally:
                _count_faults(world, stats)
                shutil.rmtree(root, ignore_errors=True)
            if position % 100 == 0:
                print(f"sim: {position}/{len(seeds)} worlds", flush=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return failures, stats, time.monotonic() - started


def _failure_groups(failures: list[_Failure]) -> list[str]:
    groups: Counter = Counter(
        (failure.invariant, failure.detail.split(":")[0]) for failure in failures
    )
    return [f"{count}x {invariant}: {detail}" for (invariant, detail), count in groups.items()]


def _failure_text(failures: list[_Failure]) -> str:
    lines = _failure_groups(failures)
    for failure in failures[:12]:
        lines.append(f"  {failure.world.describe()} -> {failure.invariant}: {failure.detail}")
    return "\n".join(lines)


def _report(failures: list[_Failure], stats: Counter, elapsed: float, seeds: list[int]) -> str:
    faults = ", ".join(
        f"{kind}={stats[kind]} (fired {stats[f'{kind}_fired']})"
        for kind in FAULT_KINDS
        if stats[kind]
    )
    lines = [
        f"worlds={len(seeds)} runtime={elapsed:.1f}s seeds={seeds[0]}..{seeds[-1]}",
        f"faults injected: {faults or 'none'} (fired={stats['fired']} "
        f"not_fired={stats['not_fired']})",
        f"rounds={stats['rounds']} ok={stats['successful_rounds']} "
        f"failed={stats['failed_rounds']} run_ok={stats['rounds'] - stats['failed_rounds']} "
        f"peak_packs={stats['peak']}",
        f"variants: empty={stats['variant_empty']} cached={stats['variant_cached']} "
        f"done={stats['variant_done']} stale={stats['variant_stale']}",
        f"retry wire overhead bytes={stats['retry_wire_overhead']} "
        f"changed set with an unchanged tree={stats['changed_false_positive']}",
    ]
    lines.append(f"FAILURES ({len(failures)}):")
    lines.extend(_failure_groups(failures))
    by_invariant: dict[str, list[int]] = {}
    for failure in failures:
        by_invariant.setdefault(failure.invariant, []).append(failure.seed)
    for invariant, seeds_hit in sorted(by_invariant.items()):
        unique = sorted(set(seeds_hit))
        lines.append(f"seeds {invariant} ({len(unique)}): {unique}")
    for failure in failures[:12]:
        lines.append(f"  {failure.world.describe()} -> {failure.invariant}: {failure.detail}")
    return "\n".join(lines)


def _seeds(count: int) -> list[int]:
    if ONLY_SEED is not None:
        return [int(ONLY_SEED)]
    return [SEED_BASE + offset for offset in range(count)]


def _assert_no_unknown(failures: list[_Failure]) -> None:
    unknown = [failure for failure in failures if not failure.known]
    if unknown:
        pytest.fail(f"pack simulation found invariant breaks\n{_failure_text(unknown)}")


def test_pack_simulation_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """20 worlds always run in the normal suite; only known product bugs may show."""
    monkeypatch.setenv("SIM_SCRATCH", str(tmp_path / "scratch"))
    seeds = _seeds(20)
    failures, stats, elapsed = run_seeds(seeds)
    print(_report(failures, stats, elapsed, seeds))
    _assert_no_unknown(failures)


@pytest.mark.skipif(not SIM_ENABLED, reason="world simulation: set NADARE_SIM=1 to run")
def test_pack_simulation_worlds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIM_SCRATCH", str(tmp_path / "scratch"))
    seeds = _seeds(DEFAULT_SEEDS)
    failures, stats, elapsed = run_seeds(seeds)
    print(_report(failures, stats, elapsed, seeds))
    _assert_no_unknown(failures)
