# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for Punishing: Gray Raven (global).

Kuro's PGR index sits behind a launcher token. It normally comes from Nadare's published
token index (``catalog/kuro-tokens.json`` in the index repo), so a rotation needs no Nadare
release. ``TOKEN`` is the offline fallback: it is what the official launcher
(``https://download.kurogames.net/transfer/pns/official/global/en/pc_app``, which serves
``pclauncher.exe``) asks for on its first start, and the index it opened matched the size of
a current Steam install to within 0.002%. The profile's ``kuro_token`` overrides both; the
value to use is the ``50015_<token>`` part of the launcher's
``.../launcher/launcher/50015_<token>/G143`` request, which shows with
``WINEDEBUG=+winhttp`` under DW-Proton.
"""

from __future__ import annotations

import re

from .. import directdl
from . import Fetch, kuro

TOKEN = "LWdk9D2Ep9mpJmqBZZkcPBU2YNraEWBQ"
EXE = "PGR.exe"
_INDEX = "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G143/50015_{token}/index.json"


def index_url(token: str = TOKEN) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", token or ""):
        raise directdl.DirectError("the Kuro token must be letters and digits only")
    return _INDEX.format(token=token)


def resolve(fetch: Fetch | None = None, token: str | None = None) -> directdl.Plan:
    return kuro.resolve_with_tokens(
        game_tag="pgr",
        group="G143",
        prefix="50015",
        builtin=TOKEN,
        token=token,
        exe=EXE,
        fetch=fetch,
        refused_hint=(
            "Kuro may have rotated Punishing: Gray Raven's download token. Update Nadare, or "
            "set the profile's kuro_token (see core/sources/pgr.py for where to find it)."
        ),
    )
