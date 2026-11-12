# SPDX-License-Identifier: GPL-3.0-or-later
"""An emitter exception must not leave the reader thread blocked on the output queue."""
import sys
import threading
import time

import pytest

from nadare.core import process


def _readers():
    return [t for t in threading.enumerate() if t.name == "nadare-run-reader"]


def test_a_raising_emitter_does_not_leak_the_reader_thread():
    def emit(event):
        if event.kind == "log":
            time.sleep(1.0)  # let the reader fill the 1024-slot queue and block
            raise RuntimeError("the log sink broke")

    code = "for _ in range(5000): print('x' * 100)"
    with pytest.raises(RuntimeError, match="log sink broke"):
        process.run([sys.executable, "-c", code], emit=emit)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _readers():
        time.sleep(0.02)
    assert not _readers(), "the output reader thread was left blocked on a full queue"
