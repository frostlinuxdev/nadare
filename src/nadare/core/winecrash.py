# SPDX-License-Identifier: GPL-3.0-or-later
"""Recognise Wine's faulting instruction and the module ranges kept by #1724."""

from __future__ import annotations

import re
from collections import deque

_CRASH = re.compile(r"^(?:wine:\s*)?Unhandled (?:page fault|exception)\b")
_ADDRESS = re.compile(
    r"\bat address\s+(?:0x)?([0-9a-fA-F]{8,16})\b"
    r"|\bin (?:32|64)-bit code\s+\(0x([0-9a-fA-F]{8,16})\)"
)
_MODULE = re.compile(
    r"^\s*(?:PE(?:-Wine)?|ELF)\s+([0-9a-fA-F]{8,16})-([0-9a-fA-F]{8,16})"
    r"\s+.+?\s+(\S+)\s*$"
)


class WineCrash:
    """Consume complete output lines, retaining only bounded module metadata.

    The process reader assembles lines across pipe reads. Module tables can precede
    or follow the exception; resolve on demand once the launch ends.
    """

    MAX_MODULES = 4096
    MAX_LINE = 4096

    def __init__(self) -> None:
        self._address: int | None = None
        self._modules: deque[tuple[int, int, str]] = deque(maxlen=self.MAX_MODULES)

    def feed(self, line: str) -> None:
        if len(line) > self.MAX_LINE:
            return
        if _CRASH.match(line):
            address = _ADDRESS.search(line)
            if address:
                self._address = int(address[1] or address[2], 16)
        module = _MODULE.match(line)
        if module:
            self._modules.append((int(module[1], 16), int(module[2], 16), module[3]))

    @property
    def detected(self) -> bool:
        return self._address is not None

    @property
    def module(self) -> str:
        if self._address is not None:
            for start, end, name in reversed(self._modules):
                if start <= self._address < end:
                    return name
        return ""
