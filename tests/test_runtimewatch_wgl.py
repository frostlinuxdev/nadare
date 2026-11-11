"""A game that cannot get OpenGL says why (#132).

On a host without libglvnd (seen in a Fedora container with NVIDIA, Proton run directly because
umu-run was missing), the game printed ``GLFW error 65542 ... WGL: The driver does not appear to
support OpenGL`` and the page only said "exit code N". The launch now names the cause.
"""

from __future__ import annotations

import pytest

from nadare.core.runtimewatch import RuntimeWatch

LINES = [
    "GLFW error 65544: WGL: Failed to set pixel format for dummy context: File not found",
    "GLFW error 65542: WGL: The driver does not appear to support OpenGL",
]


def test_the_wgl_line_is_recognised():
    watch = RuntimeWatch()
    watch.feed("some other output")
    assert not watch.no_opengl
    watch.feed(LINES[1])
    assert watch.no_opengl


def test_the_pixel_format_line_alone_is_not_enough():
    watch = RuntimeWatch()
    watch.feed(LINES[0])
    assert not watch.no_opengl


def test_the_hint_names_doctor_and_umu():
    watch = RuntimeWatch()
    watch.feed(LINES[1])
    text = watch.explain("Game could not start (exit code 1).")
    assert text.startswith("Game could not start (exit code 1).")
    assert "OpenGL" in text and "nadare doctor" in text and "umu" in text


def test_no_hint_without_the_line():
    assert RuntimeWatch().explain("x") == "x"


# ---- the launch reports it ----------------------------------------------------------------
from _backend_helpers import make_context  # noqa: E402

from nadare.backends import direct  # noqa: E402
from nadare.core.backends import BackendError  # noqa: E402


def test_a_quick_exit_after_the_wgl_line_fails_with_the_hint(tmp_path):
    backend = direct.StarRailBackend(make_context(tmp_path, "direct-hsr", {}))
    backend._watch = RuntimeWatch()
    for line in LINES:
        backend._watch.feed(line)
    backend._launch_started = None  # ran for 0 s
    with pytest.raises(BackendError, match="OpenGL"):
        backend._checked_launch_exit(1)
