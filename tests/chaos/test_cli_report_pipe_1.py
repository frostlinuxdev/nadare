# SPDX-License-Identifier: GPL-3.0-or-later
"""A closed stdout pipe must not turn ``report --stdout`` into a traceback."""

from __future__ import annotations

import io
import sys

import pytest

from nadare import app
from nadare.core.config import Config


class _ClosedPipe(io.TextIOBase):
    """Stand-in for the write end of a pipe after the reader (``| head``) has gone."""

    encoding = "utf-8"

    def write(self, text: str) -> int:  # noqa: ARG002 - the text is deliberately dropped
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **kwargs: object) -> None:  # noqa: ARG002
        pass


def test_report_stdout_handles_a_pipe_that_closes_early(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("g", backend="wine", title="G")
    config.save()

    pipe = _ClosedPipe()
    monkeypatch.setattr(sys, "stdout", pipe)
    try:
        code = app.run_cli(["-c", str(path), "report", "--stdout"])
    except BrokenPipeError as exc:
        pytest.fail(
            "run_cli let BrokenPipeError escape instead of handling the closed pipe "
            f"(the real CLI prints a traceback): {exc!r}"
        )

    print("return code with a closed stdout pipe:", code, file=sys.stderr)
    assert isinstance(code, int)
