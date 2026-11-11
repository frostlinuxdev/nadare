"""Background image threads must not outlive the test that started them (#1024).

A news-image download left running by one test was still decoding WebP inside Qt while the
next test (test_single_instance) spun an event loop; the worker aborted on CI 2026-11-10.
The autouse fixture in conftest.py joins these threads; the second test checks that it did.
Both tests live in one file so ``--dist loadfile`` keeps them in order on one worker.
"""

from __future__ import annotations

import threading

_STARTED: list[threading.Thread] = []


def test_a_test_leaves_a_news_image_thread_running():
    gate = threading.Event()
    t = threading.Thread(target=gate.wait, args=(1.0,), name="nadare-news-image", daemon=True)
    t.start()
    _STARTED.append(t)


def test_the_next_test_does_not_see_it():
    assert _STARTED, "run with the test above"
    assert not _STARTED[0].is_alive(), "the fixture did not wait for the news-image thread"
