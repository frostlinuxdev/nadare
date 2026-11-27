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
    r"authorization|captcha[-_]?token|rtCredential|api[-_]?key|token|"
    # Sign-in cookies, login tickets and session ids are as good as the password (#1593).
    # The old unbounded ``[a-z]*`` backtracked over a whole 20 KB token at every
    # start position (quadratic, seconds per line); a real identifier prefix is short.
    r"(?:set-)?cookie|ticket|[a-z]{0,32}sessid|session[-_]?(?:id|key)|"
    # Plain ``secret=`` / ``passwd=`` pairs (#1875 review); not ``pass``, which is in "bypass".
    r"secret|passwd)"
)

#: A signed or sign-in URL carries its secret in a named query parameter: a CDN or S3
#: signature, an OAuth ``code``/``state``, a device key. Only inside a URL query (after ``?``
#: or ``&``, also percent-encoded inside another URL), so ``exit code=1`` stays readable.
_QUERY_SECRET_RE = re.compile(
    r"(?i)((?:[?&]|%3F|%26)"
    r"(?:sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token|code|state|key|"
    r"secret|credential|passwd|pwd|device[-_]?key|pf_key|access[-_]?token|auth[-_]?key|sign)"
    r"(?:=|%3D))"
    r"""((?:(?!%26)[^&#\s"'])+)"""
)

#: A bare JSON Web Token (header.payload.signature), wherever it sits.
_JWT_RE = re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")


def _mask_urls_and_jwts(text: str) -> str:
    out = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + _REDACTED, text)
    return _JWT_RE.sub(_REDACTED, out)

#: An HTTP cookie header carries several ``name=value;`` pairs: an unquoted value runs to the
#: end of the line, a quoted one to its closing quote, so no pair after the first leaks (#1593).
_COOKIE_RE = re.compile(
    r"""(?i)\b((?:set-)?cookie)(["']?\s*[:=]\s*)"""
    r"""(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\r\n]*)"""
)


def _mask_cookies(text: str) -> str:
    return _COOKIE_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", text)


#: A ``key=value`` / ``key: value`` pair whose key names a secret.  The whole
#: value is redacted: a short or punctuation-bearing secret would otherwise slip
#: past the length heuristic below.
_TOKEN_VALUE_RE = re.compile(
    _TOKEN_KEYS.pattern
    # A quoted value runs to its closing quote (escapes allowed) plus anything glued to it,
    # so a secret that itself contains a quote does not leak its tail.
    # A JSON or repr key is quoted, so one closing quote may sit before the separator (#924).
    + r"""["']?\s*[:=]\s*((?:(?:Bearer|Basic|Token|Digest)\s+)?"""
    + r"""(?:"(?:[^"\\]|\\.)*"\S*|'(?:[^'\\]|\\.)*'\S*|\S+))"""
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

#: A 12-23 character run made only of readable words and short numbers, joined by ``-``/``_``,
#: is a name (``GE-Proton9-20``, ``WutheringWaves``, ``UnityPlayer_x64``), not a secret (#242).
#: A word is lower case, Capitalised, CamelCase with at most three humps, or up to five
#: capitals, optionally followed by up to three digits; a number has at most four digits.
#: Generated values (hex, base64, a word glued to a long number) do not split that way.
_READABLE_PART = r"(?:\d{1,4}|(?:[A-Z]{1,5}|[A-Z]?[a-z]+(?:[A-Z][a-z]{2,}){0,2})\d{0,3})"
_READABLE_RE = re.compile(rf"{_READABLE_PART}(?:[-_]{_READABLE_PART})*")

#: One pass matching either a placeholder (kept verbatim) or a secret-shaped run.
_SECRET_OR_PLACEHOLDER_RE = re.compile(
    f"(?:{_PLACEHOLDER_RE.pattern})|(?:{_LONG_SECRET_RE.pattern})"
)

#: A run log's crash signal name (``signal=SIGSEGV``) is short but carries upper case, so the
#: heuristic would mask it; a log that cannot name the crash is useless (#1126).
_KNOWN_SIGNAL_RE = re.compile(r"signal=SIG(?:ILL|ABRT|BUS|FPE|SEGV)")

#: Lines Wine and winedbg print for a crash: the page fault or exception line, a backtrace frame,
#: the register dump, the module list and the faulting instruction. Their hex addresses say
#: which module crashed and whether it was a null pointer; they are not secrets (#1724).
_WINE_CRASH_LINE_RE = re.compile(
    r"Unhandled (?:page fault|exception)"
    r"|^\s*(?:=>)?\s*\d+ 0x[0-9a-fA-F]+ in "
    r"|^\s*(?:[re]?[a-z]{2}|r\d{1,2}|eflags):[0-9a-fA-F]{4,16}(?:\s|$)"
    r"|^\s*(?:PE(?:-Wine)?|ELF)\s+[0-9a-fA-F]+-[0-9a-fA-F]+\s"
    r"|^0x[0-9a-fA-F]+ \S+\+0x[0-9a-fA-F]+: "
)

#: A hex address, a ``start-end`` module range or a ``module+0xoffset``, as such a line prints it.
_HEX_ADDRESS_RE = re.compile(
    r"(?:[A-Za-z_]\w*\+0x)?(?:0x)?[0-9a-fA-F]+(?:-(?:0x)?[0-9a-fA-F]+)?"
)

_REDACTED = "<redacted>"

#: ``user:password@`` credentials embedded in a URL must never be logged; the
#: password is redacted while the rest of the URL stays readable.
_USERINFO_RE = re.compile(r"(?<=//)[^/@:\s]*:[^/@\s]+@")


#: A command-line flag whose name marks a secret (``--password``, ``--auth-token``, ``-pw``).
_SECRET_FLAG_NAME = (
    r"(?:[\w-]*?" + _TOKEN_KEYS.pattern.removeprefix("(?i)") + r"[\w-]*|passwd|pass|pwd|pw)"
)
_SECRET_FLAG_RE = re.compile(r"(?i)(?:--?|/)" + _SECRET_FLAG_NAME)

#: ``--password hunter2``: the argument after such a flag is its value (#1592). The value is
#: masked unless it is another flag; ``key=value`` is :data:`_TOKEN_VALUE_RE`'s job.
_FLAG_VALUE_RE = re.compile(
    r"(?i)(?<!\S)((?:--?|/)" + _SECRET_FLAG_NAME + r"\s+)"
    r"""(?!-)(?:"(?:[^"\\]|\\.)*"\S*|'(?:[^'\\]|\\.)*'\S*|\S+)"""
)


def is_secret_flag(arg: Any) -> bool:
    """True when ``arg`` is a flag (``--token``) whose next argument is a secret (#1592)."""
    return bool(_SECRET_FLAG_RE.fullmatch(str(arg)))


def _mask_flag_values(text: str) -> str:
    return _FLAG_VALUE_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", text)


#: A setting named ``*_token``, ``*_secret``, ``*_password`` or ``*_key`` holds a secret too
#: (``nm_device_key``, ``sgdb_api_key``, #811).
_SECRET_SUFFIX = re.compile(r"(?i).+[_-](token|secret|password|passwd|key|ticket|cookie)")


def is_secret_key(key: Any) -> bool:
    """True when ``key`` names a value that must never be logged verbatim."""
    text = str(key)
    return bool(
        _TOKEN_KEYS.fullmatch(text)
        or _SECRET_SUFFIX.fullmatch(text)
        # ``_TOKEN_KEYS`` bounds the letters before ``sessid`` (linear masking); a key
        # name is short, so check the suffix itself too.
        or (text.isalpha() and text.lower().endswith("sessid"))
    )


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
        self._skip = False
        self._cancel_count = 0

    def cancel(self) -> None:
        self._cancel_count += 1
        self._cancelled = True

    @property
    def cancel_count(self) -> int:
        """How many times Stop was pressed: a second press skips the polite close."""
        return self._cancel_count

    def skip_step(self) -> None:
        """Ask the running step to stop on its own while the operation carries on.

        Unlike :meth:`cancel` this never aborts the operation; only a step that offers a skip
        (it says so with ``data={"skippable": True}``) looks at it.
        """
        self._skip = True

    @property
    def skip_requested(self) -> bool:
        return self._skip

    def reset_skip(self) -> None:
        self._skip = False

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
    out = _mask_cookies(_USERINFO_RE.sub(f"{_REDACTED}@", _mask_urls_and_jwts(text)))
    out = _TOKEN_VALUE_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", out)
    return _mask_flag_values(out)


_TRACEBACK_STRUCTURE = (
    "Traceback (most recent call last)",
    "Stack (most recent call last)",
    "During handling of the above exception",
    "The above exception was the direct cause",
)
_EXCEPTION_LINE = re.compile(r"^([A-Za-z_][\w.]*)(?:(: )(.*))?$")


def mask_traceback(text: str) -> str:
    """Mask a formatted traceback but keep what makes it readable.

    Frames and source lines are left alone, and an exception line keeps its type
    (``FileNotFoundError: ...``): only the message is masked, so the log can still be searched
    for the exception, while a token inside the message does not reach the file.
    """
    out: list[str] = []
    for line in text.split("\n"):
        if not line or line.startswith(_TRACEBACK_STRUCTURE):
            out.append(line)
            continue
        if line[0] in " \t":
            # Frames and source lines, but also the continuation of a multi-line exception
            # message: mask what is known to be secret and keep the rest readable (#968).
            out.append(mask_known(line))
            continue
        match = _EXCEPTION_LINE.match(line)
        if match is None:
            out.append(mask(line))
        elif match.group(2) is None:
            out.append(line)  # a bare exception name
        else:
            out.append(f"{match.group(1)}{match.group(2)}{mask(match.group(3))}")
    return "\n".join(out)


def _on_wine_crash_line(match: re.Match[str]) -> bool:
    """True when ``match`` sits on a line Wine prints for a crash (#1724)."""
    text = match.string
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    line = text[start : len(text) if end < 0 else end]
    return bool(_WINE_CRASH_LINE_RE.search(line))


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
        if value.startswith("%(") or (
            len(value) < 24
            and _READABLE_RE.fullmatch(value)
            and any(character.islower() for character in value)
        ):
            return value
        if _KNOWN_SIGNAL_RE.fullmatch(value):
            return value
        if _HEX_ADDRESS_RE.fullmatch(value) and _on_wine_crash_line(match):
            return value
        return _masked_value(value)

    out = _mask_cookies(_USERINFO_RE.sub(f"{_REDACTED}@", _mask_urls_and_jwts(text)))
    out = _TOKEN_VALUE_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", out)
    out = _mask_flag_values(out)
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
