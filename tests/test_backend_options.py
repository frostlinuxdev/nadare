# SPDX-License-Identifier: GPL-3.0-or-later
"""The per-backend Settings option schema.

Every registered backend declares a ``options`` tuple whose keys are real
profile settings (they must appear in the backend's own source) and unique,
and every option asks for a widget kind the Settings page knows how to draw.
"""

from __future__ import annotations

import inspect

import pytest

from nadare.core.backends import OPTION_KINDS, Option, get, kinds


def test_option_kind_is_validated() -> None:
    with pytest.raises(ValueError):
        Option("x", "X", "nonsense")
    with pytest.raises(ValueError):
        Option("x", "X", "choice")  # choice without choices


def test_every_registered_backend_declares_unique_real_options() -> None:
    for kind in kinds():
        cls = get(kind)
        options = cls.options
        assert options, f"{kind} has no options"
        keys = [option.key for option in options]
        assert len(keys) == len(set(keys)), f"{kind} has duplicate option keys"
        assert len({option.label for option in options}) == len(keys), f"{kind} labels"
        source = inspect.getsource(inspect.getmodule(cls))
        for option in options:
            assert option.kind in OPTION_KINDS
            assert option.key in source, f"{kind}: {option.key!r} is not a real key"
            if option.kind == "choice":
                assert option.choices


def test_expected_backends_are_covered() -> None:
    for kind in (
        "stove-czn",
        "fgo-lepton",
        "arknights-yostar",
        "stellasora-yostar",
        "wine",
        "steam",
        "script",
        "waydroid",
    ):
        assert get(kind).options


def test_download_workers_is_wired_into_yostar_sync() -> None:
    from nadare.backends.arknights import ArknightsBackend

    assert "download_workers" in {option.key for option in ArknightsBackend.options}


def test_wine_and_proton_backends_offer_a_renderer_choice() -> None:
    for kind in ("stove-czn", "arknights-yostar", "stellasora-yostar", "wine"):
        options = {option.key: option for option in get(kind).options}
        renderer = options["renderer"]
        assert renderer.kind == "choice"
        assert renderer.default == "auto"
        assert {value for _label, value in renderer.choices} == {"auto", "vulkan", "opengl"}
