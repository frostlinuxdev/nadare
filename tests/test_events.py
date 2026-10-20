# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for nadare.core.events."""

from __future__ import annotations

import pytest

from nadare.core.events import (
    Cancel,
    Cancelled,
    Event,
    contains_value,
    emitter,
    iter_strings,
    mask,
)


def test_iter_strings_walks_nested_payloads() -> None:
    payload = {
        "a": "one",
        "b": ["two", {"c": "three"}],
        "d": 4,
    }
    assert list(iter_strings(payload)) == ["one", "two", "three"]
    assert list(iter_strings("solo")) == ["solo"]
    assert list(iter_strings(4)) == []


def test_contains_value_finds_secret_in_message_and_data() -> None:
    secret = "TOKEN-abcdef0123456789"
    assert contains_value(Event("log", f"using {secret}"), secret) is True
    assert (
        contains_value(Event("log", "ok", data={"nested": ["x", {"t": secret}]}), secret)
        is True
    )
    assert contains_value(Event("log", "clean", data={"t": "other"}), secret) is False
    assert contains_value(Event("log", "clean"), secret) is False


def test_event_defaults() -> None:
    event = Event("status")
    assert event.kind == "status"
    assert event.message == ""
    assert event.progress is None
    assert event.data == {}
    # Mutable default is per-instance, not shared.
    other = Event("log")
    event.data["x"] = 1
    assert other.data == {}


def test_emitter_none_returns_noop() -> None:
    noop = emitter(None)
    assert noop(Event("log", "ignored")) is None


def test_emitter_passthrough() -> None:
    seen: list[Event] = []
    fn = emitter(seen.append)
    fn(Event("log", "hello"))
    assert seen == [Event("log", "hello")]


def test_cancel_token() -> None:
    cancel = Cancel()
    assert cancel.cancelled is False
    cancel.raise_if_cancelled()  # no-op before cancel

    cancel.cancel()
    assert cancel.cancelled is True
    with pytest.raises(Cancelled):
        cancel.raise_if_cancelled()


def test_mask_never_reveals_a_full_secret() -> None:
    secret = "abcdefghijklmnopqrstuvwxyz012345"  # 32 chars, all in the class
    text = f"Bearer {secret} end"

    masked = mask(text)

    assert secret not in masked
    assert secret[:4] in masked
    assert secret[-4:] in masked
    assert masked == f"Bearer {secret[:4]}…{secret[-4:]}<32> end"


def test_mask_redacts_named_secret_keys() -> None:
    """L1: a secret shorter than the length heuristic is still redacted when it
    follows a known secret-bearing key, and the key name is not revealed."""
    for text in (
        "password=hunter2",
        "access_token: abc.def-ghi",
        "refresh_token=short",
        "client_secret='quoted'",
    ):
        masked = mask(text)
        assert "hunter2" not in masked
        assert "abc.def-ghi" not in masked
        assert "<redacted>" in masked


def test_mask_threshold_catches_mid_length_tokens() -> None:
    """L1: the old 24-char threshold let 12–23 char secrets through verbatim."""
    secret = "Abcdef123456"  # 12 chars, secret-shaped
    assert secret not in mask(f"value {secret} end")


def test_mask_masks_every_secret_in_a_message() -> None:
    one = "A" * 24
    two = "7" + "b" * 39  # secret-shaped (carries a digit)
    masked = mask(f"first={one} second={two}")

    assert one not in masked
    assert two not in masked
    # Each long value was replaced with a masked form.
    assert masked.count("…") == 2
    assert "<24>" in masked or "<30>" in masked
    assert "bbbb" in masked


def test_mask_leaves_ordinary_text_untouched() -> None:
    text = "downloading 1024 bytes to /home/user/game.bin"
    assert mask(text) == text


def test_mask_preserves_named_placeholders() -> None:
    """NV-5: a ``%(name)s`` placeholder must survive so ``%``-formatting works."""
    text = "update %(correlation_id)s failed for %(profile_name)-10s"
    assert mask(text) == text


def test_mask_leaves_plain_lowercase_identifiers() -> None:
    """NV-5: a benign lowercase word is not secret-shaped, so it is left alone."""
    text = "profile lowercasename selected"
    assert mask(text) == text


def test_mask_handles_base64_padding() -> None:
    # '=' and '+' are valid base64 characters and must be included in the match.
    secret = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo+PQ=="
    masked = mask(secret)
    assert secret not in masked
    assert masked.startswith(secret[:4])
    assert masked.endswith(f"{secret[-4:]}<{len(secret)}>")


def test_mask_redacts_bearer_scheme():
    from nadare.core.events import mask

    assert "password123" not in mask("Authorization: Bearer password123")
    assert "secret99" not in mask("authorization=Basic secret99")
