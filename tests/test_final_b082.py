# SPDX-License-Identifier: GPL-3.0-or-later
"""run(forward_stderr=False) must not deadlock on a chatty stderr."""
import sys
import threading

import pytest

from nadare.core import process
from nadare.core.events import Cancel, Cancelled


def test_run_with_forward_stderr_false_reads_or_discards_stderr():
    code = "import sys\nsys.stderr.write('e' * 300000)\nsys.stderr.flush()\n"
    cancel = Cancel()
    timer = threading.Timer(10.0, cancel.cancel)  # a deadlock fails the test, not the suite
    timer.start()
    try:
        cp = process.run(
            [sys.executable, "-c", code], forward_stderr=False, cancel=cancel
        )
    except Cancelled:
        pytest.fail("the child blocked on its full stderr pipe: run() never consumed it")
    finally:
        timer.cancel()
    assert cp.returncode == 0
