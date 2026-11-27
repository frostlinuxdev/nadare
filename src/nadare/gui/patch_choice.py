# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""What Nadare asks before the English patch switch turns on (#1866).

Turning it off needs no question: it only puts the game's own files back.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.patches import PatchSpec


@dataclass(frozen=True)
class PatchOffer:
    title: str
    text: str
    button: str


def offer(spec: PatchSpec) -> PatchOffer:
    return PatchOffer(
        title=spec.title,
        text=(
            f"Turn on {spec.title} {spec.version}? It is an unofficial community translation "
            "(KiboTL) that replaces some of the game's text files. The publisher may treat a "
            "modified client as a rules break, so there is a ban risk.\n\n"
            "It only works with the game build it was made for. A game update or Repair "
            "files undoes it; turn it on again afterwards. Turning it off puts the original "
            "files back."
        ),
        button="Turn on",
    )


__all__ = ["PatchOffer", "offer"]
