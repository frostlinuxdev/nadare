# SPDX-License-Identifier: GPL-3.0-or-later
"""The opt-in catalog section of scripts/publish_index.py (no network: resolvers and the image
check are injected)."""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_index.py"


@pytest.fixture(scope="module")
def pi():
    spec = importlib.util.spec_from_file_location("publish_index_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SITE = "https://site.example/images"


def games(pi, versions: dict[str, object]):
    """Two fake games; a value that is an Exception instance is raised by that resolver."""

    def resolver(tag):
        def resolve():
            value = versions[tag]
            if isinstance(value, Exception):
                raise value
            return value

        return resolve

    return {
        "alpha": pi.CatalogGame("Alpha Game", resolver("alpha"), f"{SITE}/alpha", 111),
        "beta": pi.CatalogGame("Beta Game", resolver("beta"), f"{SITE}/beta", None),
    }


def always_ok(url: str) -> int:
    return 200


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_publishes_an_index_and_one_media_file_per_game(pi, tmp_path):
    out = tmp_path / "catalog"
    assert pi.publish_catalog(
        out, {}, games=games(pi, {"alpha": "1.2", "beta": "9"}), check=always_ok
    )
    index = read(out / "index.json")
    assert set(index) == {"alpha", "beta"}
    assert index["alpha"] == {"title": "Alpha Game", "version": "1.2", "media": "alpha.media.json"}
    media = read(out / "alpha.media.json")
    assert media["game_tag"] == "alpha" and media["title"] == "Alpha Game"
    assert (
        media["hero"] == f"{SITE}/alpha/hero.webp" and media["thumb"] == f"{SITE}/alpha/thumb.webp"
    )
    assert media["steam_appid"] == 111
    assert "steam_appid" not in read(out / "beta.media.json")
    assert (out / "index.json").read_text().endswith("\n")


def test_an_image_that_is_missing_upstream_is_left_out(pi, tmp_path):
    out = tmp_path / "catalog"
    check = lambda url: 404 if url.endswith("/hero.webp") else 200  # noqa: E731
    pi.publish_catalog(out, {}, games=games(pi, {"alpha": "1", "beta": "1"}), check=check)
    media = read(out / "alpha.media.json")
    assert "hero" not in media and media["thumb"].endswith("/thumb.webp")


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_a_transient_server_error_keeps_the_last_good_images(pi, tmp_path, status):
    out = tmp_path / "catalog"
    pi.publish_catalog(out, {}, games=games(pi, {"alpha": "1", "beta": "1"}), check=always_ok)
    changed = pi.publish_catalog(
        out, {}, games=games(pi, {"alpha": "1", "beta": "1"}), check=lambda url: status
    )
    media = read(out / "alpha.media.json")
    assert changed is False and media["hero"].endswith("/hero.webp") and media["thumb"]


def test_an_unreachable_image_check_keeps_the_last_good_value(pi, tmp_path):
    out = tmp_path / "catalog"
    pi.publish_catalog(out, {}, games=games(pi, {"alpha": "1", "beta": "1"}), check=always_ok)

    def broken(url: str) -> int:
        raise OSError("network down")

    changed = pi.publish_catalog(
        out, {}, games=games(pi, {"alpha": "1", "beta": "1"}), check=broken
    )
    assert changed is False
    assert read(out / "alpha.media.json")["hero"].endswith("/hero.webp")


def test_an_unchanged_second_run_rewrites_nothing(pi, tmp_path):
    out = tmp_path / "catalog"
    g = games(pi, {"alpha": "1", "beta": "1"})
    pi.publish_catalog(out, {}, games=g, check=always_ok, now=1_000)
    before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in out.iterdir()}
    time.sleep(0.01)
    assert pi.publish_catalog(out, {}, games=g, check=always_ok, now=999_999) is False
    assert {p.name: (p.stat().st_ino, p.read_bytes()) for p in out.iterdir()} == before


def test_a_new_version_updates_the_index_and_reports_a_change(pi, tmp_path):
    out = tmp_path / "catalog"
    pi.publish_catalog(out, {}, games=games(pi, {"alpha": "1", "beta": "1"}), check=always_ok)
    assert pi.publish_catalog(
        out, {}, games=games(pi, {"alpha": "2", "beta": "1"}), check=always_ok
    )
    assert read(out / "index.json")["alpha"]["version"] == "2"
    assert read(out / "index.json")["beta"]["version"] == "1"


def test_a_failing_game_keeps_its_published_entry(pi, tmp_path, capsys):
    out = tmp_path / "catalog"
    pi.publish_catalog(out, {}, games=games(pi, {"alpha": "1", "beta": "1"}), check=always_ok)
    changed = pi.publish_catalog(
        out,
        {},
        games=games(pi, {"alpha": RuntimeError("API refused"), "beta": "2"}),
        check=always_ok,
    )
    index = read(out / "index.json")
    assert changed and index["alpha"]["version"] == "1" and index["beta"]["version"] == "2"
    assert "alpha" in capsys.readouterr().out


def test_a_game_that_never_resolved_is_not_published(pi, tmp_path):
    out = tmp_path / "catalog"
    pi.publish_catalog(
        out, {}, games=games(pi, {"alpha": RuntimeError("x"), "beta": "3"}), check=always_ok
    )
    assert set(read(out / "index.json")) == {"beta"}
    assert not (out / "alpha.media.json").exists()


def test_an_all_failing_first_run_publishes_nothing(pi, tmp_path):
    out = tmp_path / "catalog"
    g = games(pi, {"alpha": RuntimeError("x"), "beta": RuntimeError("y")})
    assert pi.publish_catalog(out, {}, games=g, check=always_ok) is False
    assert not (out / "index.json").exists()  # never an empty index


@pytest.mark.parametrize("version", ["", None, 5])
def test_a_resolver_that_returns_no_usable_version_counts_as_failed(pi, tmp_path, version):
    out = tmp_path / "catalog"
    pi.publish_catalog(out, {}, games=games(pi, {"alpha": version, "beta": "1"}), check=always_ok)
    assert set(read(out / "index.json")) == {"beta"}


def test_the_age_gate(pi, tmp_path):
    state = tmp_path / "state" / "catalog-checked"
    assert pi.catalog_due(state, max_age_hours=6, now=10_000)  # never run
    pi.mark_catalog_checked(state, now=10_000)
    assert state.is_file()
    assert not pi.catalog_due(state, max_age_hours=6, now=10_000 + 3600)
    assert pi.catalog_due(state, max_age_hours=6, now=10_000 + 7 * 3600)
    os.utime(state, (10_000, 10_000))
    assert pi.catalog_due(state, max_age_hours=0, now=10_000)  # 0 = always


@pytest.fixture
def quiet_main(pi, monkeypatch, tmp_path):
    """main() with every network step stubbed; records what ran."""
    ran = []
    for name in ("publish_yostar", "publish_czn", "publish_webview2", "publish_vcredist"):
        monkeypatch.setattr(pi, name, lambda *a, _n=name, **k: ran.append(_n) or False)
    monkeypatch.setattr(pi, "publish_catalog", lambda *a, **k: ran.append("catalog") or True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return ran


def test_the_catalog_is_off_unless_asked_for(pi, quiet_main, tmp_path):
    assert pi.main(["--index", str(tmp_path / "idx")]) == pi.UNCHANGED
    assert "catalog" not in quiet_main


def test_catalog_flag_runs_it_once_then_the_age_gate_skips_it(pi, quiet_main, tmp_path):
    assert pi.main(["--index", str(tmp_path / "idx"), "--catalog"]) == 0
    assert quiet_main.count("catalog") == 1
    assert (
        pi.main(["--index", str(tmp_path / "idx"), "--catalog"]) == pi.UNCHANGED
    )  # gated: nothing new
    assert quiet_main.count("catalog") == 1
    assert pi.main(["--index", str(tmp_path / "idx"), "--catalog", "--catalog-force"]) == 0
    assert quiet_main.count("catalog") == 2


def test_a_failing_catalog_does_not_stop_the_other_steps_or_start_the_age_gate(
    pi, monkeypatch, quiet_main, tmp_path, capsys
):
    def boom(*a, **k):
        raise RuntimeError("publisher down")

    monkeypatch.setattr(pi, "publish_catalog", boom)
    # the other steps ran and main() still finishes normally
    assert pi.main(["--index", str(tmp_path / "idx"), "--catalog"]) == pi.UNCHANGED
    assert "publish_vcredist" in quiet_main
    assert "publisher down" in capsys.readouterr().err
    monkeypatch.setattr(pi, "publish_catalog", lambda *a, **k: quiet_main.append("catalog") or True)
    assert pi.main(["--index", str(tmp_path / "idx"), "--catalog"]) == 0  # not gated by the failure


def test_the_default_table_covers_every_visible_catalog_game_with_a_resolver(pi, monkeypatch):
    from nadare.core import directkinds as dk

    monkeypatch.delenv("NADARE_SHOW_HOYO", raising=False)
    table = pi.default_catalog_games()
    kinds = {k for k in dk.ART if k != "launcher-nikke" and k != "launcher-nte"}
    assert {f"direct-{tag}" for tag in table} == kinds - dk.HOYO_KINDS
    for tag, game in table.items():
        assert callable(game.resolve) and game.title
        folder, steam = dk.ART[f"direct-{tag}"]
        assert game.image_base == f"{dk.SITE_IMAGES}/{folder}" and game.steam_appid == steam


def test_a_missing_image_keeps_the_previously_published_one(pi, tmp_path):
    """A 404 can be a Pages deploy in flight: never strip a good image because of one bad check."""
    out = tmp_path / "catalog"
    g = games(pi, {"alpha": "1", "beta": "1"})
    pi.publish_catalog(out, {}, games=g, check=always_ok)
    assert pi.publish_catalog(out, {}, games=g, check=lambda url: 404) is False
    assert read(out / "alpha.media.json")["hero"].endswith("/hero.webp")


def test_a_corrupt_published_index_is_rebuilt_instead_of_wedging_the_run(pi, tmp_path, capsys):
    out = tmp_path / "catalog"
    out.mkdir()
    (out / "index.json").write_text("{ truncated", encoding="utf-8")
    assert pi.publish_catalog(
        out, {}, games=games(pi, {"alpha": "1", "beta": "2"}), check=always_ok
    )
    assert set(read(out / "index.json")) == {"alpha", "beta"}
    assert "rebuilding" in capsys.readouterr().out


def test_a_corrupt_media_file_is_rewritten(pi, tmp_path):
    out = tmp_path / "catalog"
    pi.publish_catalog(out, {}, games=games(pi, {"alpha": "1", "beta": "1"}), check=always_ok)
    (out / "alpha.media.json").write_text("", encoding="utf-8")
    assert pi.publish_catalog(
        out, {}, games=games(pi, {"alpha": "1", "beta": "1"}), check=always_ok
    )
    assert read(out / "alpha.media.json")["game_tag"] == "alpha"


def test_the_deadline_leaves_games_it_did_not_reach_untouched(pi, tmp_path, capsys):
    out = tmp_path / "catalog"
    pi.publish_catalog(out, {}, games=games(pi, {"alpha": "1", "beta": "1"}), check=always_ok)
    ticks = iter([0.0, 1.0, 999.0, 999.0, 999.0])  # alpha is reached, beta is not
    changed = pi.publish_catalog(
        out,
        {},
        games=games(pi, {"alpha": "2", "beta": "2"}),
        check=always_ok,
        deadline=10,
        clock=lambda: next(ticks),
    )
    index = read(out / "index.json")
    assert changed and index["alpha"]["version"] == "2" and index["beta"]["version"] == "1"
    assert "deadline" in capsys.readouterr().out


def test_a_kind_without_a_resolver_is_skipped_not_fatal(pi, monkeypatch, capsys):
    from nadare.core import directkinds as dk

    monkeypatch.setitem(dk.ART, "direct-futuregame", ("future", None))
    table = pi.default_catalog_games()
    assert "futuregame" not in table and "wuwa" in table
    assert "futuregame" in capsys.readouterr().out


def test_the_refresh_script_has_the_catalog_switch_off_by_default():
    text = (SCRIPT.parent / "refresh_index.sh").read_text(encoding="utf-8")
    assert '[[ "${NADARE_INDEX_CATALOG:-}" == 1 ]] && catalog=--catalog' in text
    assert 'publish_index.py" --index "$index" $skip_yostar $catalog' in text
    assert '[[ -d "$index/catalog" ]] && git -C "$index" add catalog' in text


def test_repeated_failures_raise_a_canary_exactly_at_the_threshold(pi, tmp_path, capsys):
    out = tmp_path / "catalog"
    state = tmp_path / "failures.json"
    g = games(pi, {"alpha": RuntimeError("API refused"), "beta": "1"})
    for run in (1, 2):
        pi.publish_catalog(out, {}, games=g, check=always_ok, failures_state=state)
        assert "CANARY" not in capsys.readouterr().out
        assert read(state)["alpha"]["count"] == run
    pi.publish_catalog(out, {}, games=g, check=always_ok, failures_state=state)
    printed = capsys.readouterr().out
    assert "CANARY: catalog source alpha has failed 3 runs in a row (API refused)" in printed
    assert read(state)["alpha"]["count"] == 3


def test_a_successful_run_clears_the_failure_count(pi, tmp_path):
    out = tmp_path / "catalog"
    state = tmp_path / "failures.json"
    pi.publish_catalog(
        out,
        {},
        games=games(pi, {"alpha": RuntimeError("down"), "beta": RuntimeError("down")}),
        check=always_ok,
        failures_state=state,
    )
    pi.publish_catalog(
        out,
        {},
        games=games(pi, {"alpha": "1", "beta": RuntimeError("down")}),
        check=always_ok,
        failures_state=state,
    )
    assert read(state) == {"beta": {"count": 2, "reason": "down"}}


def test_the_deadline_does_not_count_a_game_it_never_reached(pi, tmp_path):
    out = tmp_path / "catalog"
    state = tmp_path / "failures.json"
    pi.publish_catalog(
        out,
        {},
        games=games(pi, {"alpha": "1", "beta": RuntimeError("down")}),
        check=always_ok,
        failures_state=state,
    )
    ticks = iter([0.0, 1.0, 999.0])
    pi.publish_catalog(
        out,
        {},
        games=games(pi, {"alpha": "2", "beta": "2"}),
        check=always_ok,
        deadline=10,
        clock=lambda: next(ticks),
        failures_state=state,
    )
    assert read(state) == {"beta": {"count": 1, "reason": "down"}}


def test_a_corrupt_failure_state_is_treated_as_empty(pi, tmp_path):
    out = tmp_path / "catalog"
    state = tmp_path / "failures.json"
    state.write_text("{ truncated", encoding="utf-8")
    pi.publish_catalog(
        out,
        {},
        games=games(pi, {"alpha": RuntimeError("down"), "beta": "1"}),
        check=always_ok,
        failures_state=state,
    )
    assert read(state) == {"alpha": {"count": 1, "reason": "down"}}


def test_no_failure_state_means_no_tracking_and_no_canary(pi, tmp_path, capsys):
    out = tmp_path / "catalog"
    g = games(pi, {"alpha": RuntimeError("down"), "beta": RuntimeError("down")})
    for _ in range(4):
        pi.publish_catalog(out, {}, games=g, check=always_ok)
    assert "CANARY" not in capsys.readouterr().out
    assert not out.exists()


def test_the_stored_reason_is_truncated_to_120_characters(pi, tmp_path):
    out = tmp_path / "catalog"
    state = tmp_path / "failures.json"
    reason = "x" * 200
    pi.publish_catalog(
        out,
        {},
        games=games(pi, {"alpha": RuntimeError(reason), "beta": "1"}),
        check=always_ok,
        failures_state=state,
    )
    assert read(state)["alpha"]["reason"] == reason[:120]


def test_the_hoyo_games_are_only_published_when_asked_for(pi, monkeypatch):
    from nadare.core import directkinds as dk

    monkeypatch.delenv("NADARE_SHOW_HOYO", raising=False)
    assert not {k.removeprefix("direct-") for k in dk.HOYO_KINDS} & set(pi.default_catalog_games())
    monkeypatch.setenv("NADARE_SHOW_HOYO", "1")
    assert {k.removeprefix("direct-") for k in dk.HOYO_KINDS} <= set(pi.default_catalog_games())
