# SPDX-License-Identifier: GPL-3.0-or-later
"""#622 item 3: two Nadare processes (GUI + CLI) rotating one log lose lines.

The stdlib handler rotates by renaming with only an in-process lock: process A renames
``nadare.log`` -> ``.1`` while B still writes to its old handle, then B rotates again and the
backups shift twice (the audit saw 600 lines emitted, 450 on disk, ``.3`` missing). A
cross-process lock around write + rotation, with a reopen when the file was rotated under us,
keeps every line.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path

LINES = 600
PROCS = 4


def _writer(path: str, tag: str) -> None:
    import logging

    from nadare.core.logging import _RotatingFileHandler

    handler = _RotatingFileHandler(path, maxBytes=1024, backupCount=2000, delay=True)
    log = logging.getLogger(f"rot-{tag}")
    log.propagate = False
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    for i in range(LINES):
        log.info("%s line %04d %s", tag, i, "x" * 40)
    handler.close()


def test_no_line_is_lost_when_processes_rotate_the_same_log(tmp_path):
    log = tmp_path / "logs" / "nadare.log"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(log), f"p{n}")) for n in range(PROCS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    lines = []
    for f in log.parent.glob("nadare.log*"):
        if f.name.endswith(".lock"):
            continue
        lines += [ln for ln in f.read_text().splitlines() if " line " in ln]
    assert len(lines) == LINES * PROCS
    assert len(set(lines)) == LINES * PROCS


def test_the_lock_file_sits_next_to_the_log_and_is_private(tmp_path):
    import logging

    from nadare.core.logging import _RotatingFileHandler

    path = tmp_path / "logs" / "nadare.log"
    handler = _RotatingFileHandler(str(path), maxBytes=100, backupCount=2, delay=True)
    handler.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None))
    handler.close()
    lock = Path(str(path) + ".lock")
    assert lock.is_file()
    assert lock.stat().st_mode & 0o077 == 0


def test_a_writer_follows_a_rotation_done_by_another_process(tmp_path):
    """After another process rotated the log, the next line goes into the live log, not into
    the backup this handle still points at (two handlers stand in for two processes)."""
    import logging

    from nadare.core.logging import _RotatingFileHandler

    path = tmp_path / "logs" / "nadare.log"
    a = _RotatingFileHandler(str(path), maxBytes=200, backupCount=5, delay=True)
    b = _RotatingFileHandler(str(path), maxBytes=200, backupCount=5, delay=True)

    def rec(msg):
        return logging.LogRecord("x", logging.INFO, __file__, 1, msg, None, None)

    a.emit(rec("a first"))
    for i in range(10):  # b fills and rotates the log
        b.emit(rec(f"b {i} " + "y" * 40))
    a.emit(rec("a after rotation"))
    a.close()
    b.close()
    assert "a after rotation" in path.read_text()
