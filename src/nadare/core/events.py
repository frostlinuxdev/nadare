# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The only channel from a backend to the UI.

Backends are non-interactive and block; they report by calling an emitter.
The GUI forwards these to Qt signals; the CLI prints them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

# Never surface these verbatim.
_TOKEN_KEYS = re.compile(
    r"(?i)(access_token|refresh_token|client_secret|password|auth_code|"
    r"authorization|captcha[-_]?token|rtCredential|api[-_]?key|token)"
)

#: A ``key=value`` / ``key: value`` pair whose key names a secret.  The whole
#: value is redacted: a short or punctuation-bearing secret would otherwise slip
#: past the length heuristic below.
_TOKEN_VALUE_RE = re.compile(
    _TOKEN_KEYS.pattern + r"\s*[:=]\s*((?:(?:Bearer|Basic|Token|Digest)\s+)?\S+)"
)

#: A ``%(name)s`` / ``%(name)-10s`` placeholder must survive masking so that
#: ``%``-style logging still formats at emit time (NV-5).
_PLACEHOLDER_RE = re.compile(r"%\([^)]*\)[#0-9.\-+ ]*[a-zA-Z]")

#: Fallback heuristic. A run of 24+ characters is always treated as a secret
#: (the pre-L1 threshold), while a 12–23 character run only counts when it looks
#: *generated* — it carries a digit, an upper-case letter or base64 punctuation —
#: so a plain lower-case word (a profile name, say) is left alone. ``/`` is
#: deliberately excluded so filesystem paths are not mangled; the key-based pass
#: above covers named secrets regardless of shape.
_LONG_SECRET_RE = re.compile(
    r"[A-Za-z0-9_\-+=]{24,}|(?=[A-Za-z0-9_\-+=]*[0-9A-Z+/=])[A-Za-z0-9_\-+=]{12,}"
)

#: One pass matching either a placeholder (kept verbatim) or a secret-shaped run.
_SECRET_OR_PLACEHOLDER_RE = re.compile(
    f"(?:{_PLACEHOLDER_RE.pattern})|(?:{_LONG_SECRET_RE.pattern})"
)

_REDACTED = "<redacted>"

#: ``user:password@`` credentials embedded in a URL must never be logged; the
#: password is redacted while the rest of the URL stays readable.
_USERINFO_RE = re.compile(r"(?<=//)[^/@:\s]*:[^/@\s]+@")


def is_secret_key(key: Any) -> bool:
    """True when ``key`` names a value that must never be logged verbatim."""
    return bool(_TOKEN_KEYS.fullmatch(str(key)))


@dataclass
class Event:
    kind: str  # status | step | progress | log | done | error
    message: str = ""
    progress: float | None = None  # 0.0..1.0 when known
    data: dict[str, Any] = field(default_factory=dict)


Emitter = Callable[[Event], None]


class Cancelled(Exception):
    """Raised by a backend when a Cancel is set."""


class Cancel:
    """Cooperative cancellation token handed to backend operations."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise Cancelled()


def _masked_value(value: str) -> str:
    if len(value) < 8:
        return _REDACTED
    return f"{value[:4]}…{value[-4:]}<{len(value)}>"


def mask_known(text: str) -> str:
    """Mask only what is known to be secret: URL userinfo and ``password=``-style values.

    Unlike :func:`mask` this leaves long hashes, paths and timestamps alone, so it suits text
    people read (the log viewer) while the length heuristic suits text written to disk.
    """
    out = _USERINFO_RE.sub(f"{_REDACTED}@", text)
    return _TOKEN_VALUE_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", out)


def mask(text: str) -> str:
    """Mask anything that looks like a secret value. Used before logging.

    Known secret-bearing keys (``password=``, ``authorization:`` …) are redacted
    whole first, then a length heuristic masks any remaining secret-shaped long
    token.  ``%(name)s`` placeholders are matched as a whole and kept verbatim so
    ``%``-formatting still works.  Both passes use ``re.sub`` over fixed spans,
    so overlapping matches cannot corrupt the result the way repeated
    ``str.replace`` could.
    """

    def _replace(match: re.Match[str]) -> str:
        value = match.group(0)
        return value if value.startswith("%(") else _masked_value(value)

    out = _USERINFO_RE.sub(f"{_REDACTED}@", text)
    out = _TOKEN_VALUE_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", out)
    return _SECRET_OR_PLACEHOLDER_RE.sub(_replace, out)


def iter_strings(value: Any) -> Iterator[str]:
    """Yield every string leaf inside an event payload (message/data)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from iter_strings(item)


def contains_value(event: Event, needle: str) -> bool:
    """True when ``needle`` appears anywhere in the event (message or data).

    The CZN backend uses this in tests to prove that a sentinel token never
    reaches the GUI through an emitted event.
    """
    if not needle:
        return False
    if needle in event.message:
        return True
    return any(needle in value for value in iter_strings(event.data))


def emitter(emit: Emitter | None) -> Emitter:
    return emit if emit is not None else (lambda _e: None)
