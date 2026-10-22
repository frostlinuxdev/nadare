# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests that the logging setup never writes outside the test tmp tree."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nadare.core import logging as nadare_logging


def test_file_handler_writes_inside_test_tmp(tmp_path: Path) -> None:
    logger = logging.getLogger(nadare_logging.LOGGER_NAME)
    handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert handlers, "expected the nadare logger to have a file handler"
    for handler in handlers:
        assert Path(handler.baseFilename).is_relative_to(tmp_path), handler.baseFilename


def test_log_path_points_inside_test_tmp(tmp_path: Path) -> None:
    assert nadare_logging.log_path().is_relative_to(tmp_path)


def test_masking_filter_redacts_secret_keyed_args() -> None:
    """L1: a structured value under a secret-bearing key must be redacted."""
    record = logging.LogRecord(
        "nadare", logging.INFO, __file__, 1, "login %(token)s", {"token": "abc", "x": "y"}, None
    )
    nadare_logging.MaskingFilter().filter(record)
    assert record.args["token"] == "<redacted>"  # a bare ``token`` key is a secret (#49)

    record = logging.LogRecord(
        "nadare",
        logging.INFO,
        __file__,
        1,
        "login %(password)s",
        {"password": "hunter2", "x": "y"},
        None,
    )
    nadare_logging.MaskingFilter().filter(record)
    assert record.args["password"] == "<redacted>"


def test_setup_retries_after_handler_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """L21: a transient file-handler failure must not latch ``_configured``."""
    nadare_logging.reset()

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("read-only state dir")

    monkeypatch.setattr(nadare_logging, "_RotatingFileHandler", boom)
    nadare_logging.setup(console=False)
    assert nadare_logging._configured is False

    monkeypatch.undo()
    nadare_logging.setup(console=True)
    assert nadare_logging._configured is True
    nadare_logging.reset()


def test_masking_filter_password_percent_s():
    import logging

    from nadare.core.logging import MaskingFilter

    rec = logging.LogRecord("n", logging.WARNING, "f", 1, "password=%s", ("pw",), None)
    assert MaskingFilter().filter(rec)
    assert rec.getMessage() == "password=<redacted>"


def test_setup_creates_missing_parent_directory(tmp_path: Path) -> None:
    """L34: the file handler must create a missing log directory on first write."""
    nadare_logging.reset()
    log_file = tmp_path / "nested" / "deep" / "nadare.log"
    assert not log_file.parent.exists()

    logger = nadare_logging.setup(filename=log_file)
    message = "nested probe directory created"
    logger.info(message)

    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
    nadare_logging.reset()

    assert log_file.exists()
    assert message in log_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "key", ["token", "api_key", "api-key", "apikey", "API_KEY", "Token", "refresh_token"]
)
def test_every_named_secret_key_is_redacted(key: str) -> None:
    record = logging.LogRecord(
        "nadare", logging.INFO, __file__, 1, "x %(" + key + ")s", {key: "abc", "x": "y"}, None
    )
    nadare_logging.MaskingFilter().filter(record)
    assert record.args[key] == "<redacted>"
    assert record.args["x"] == "y"


@pytest.mark.parametrize("text", ["token=abc", "api_key: xyz", "login ok api-key=Zz9"])
def test_bare_token_and_api_key_pairs_are_masked_in_text(text: str) -> None:
    from nadare.core.events import mask

    masked = mask(text)
    assert "abc" not in masked and "xyz" not in masked and "Zz9" not in masked


@pytest.mark.parametrize(
    "text", ["max_tokens=100", "tokenizer=bert", "token_count 3", "tokens: 5"]
)
def test_the_secret_key_pattern_does_not_match_look_alike_words(text: str) -> None:
    """Only whole key names count: ``max_tokens`` or ``tokenizer`` are not secrets.
    (The separate length heuristic in ``mask`` is unrelated and unchanged.)"""
    from nadare.core import events

    assert events._TOKEN_VALUE_RE.search(text) is None
    assert events.is_secret_key(text.split("=")[0].split(":")[0].strip()) is False


def test_later_setup_with_level_applies_it(tmp_path):
    import logging

    from nadare.core import logging as nl

    nl.reset()
    nl.setup(filename=tmp_path / "n.log")
    assert nl.get_logger("x").getEffectiveLevel() == logging.INFO
    nl.setup(level=logging.DEBUG)
    assert nl.get_logger("x").isEnabledFor(logging.DEBUG)
    nl.setup()
    assert nl.get_logger("x").isEnabledFor(logging.DEBUG)
    nl.reset()
