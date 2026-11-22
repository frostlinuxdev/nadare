#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render the blue-and-black theme offscreen for review.

Builds a :class:`~nadare.gui.main_window.MainWindow` against a temporary XDG
tree with three fake profiles (no network, no real user config) and writes
``QWidget.grab()`` PNGs of the library page and a game page.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Force the headless Qt platform *before* importing PyQt6.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

OUTPUT_DIR = Path(
    os.environ.get("NADARE_THEME_SHOTS") or Path(tempfile.gettempdir()) / "nadare-theme-shots"
)

PROFILES = (
    ("neon-drift", "Neon Drift", "wine"),
    ("aether-arc", "Aether Arc", "generic"),
    ("midnight-run", "Midnight Run", "wine"),
    ("dust-veil", "Dust Veil", "wine"),
)


def _fake_news(workdir: Path):
    """Build a fake, offline ``NewsResult`` with eight local thumbnails."""
    from nadare.core.news import NewsResult
    from nadare.gui.theme import placeholder_art

    assets = workdir / "news-thumbs"
    assets.mkdir(parents=True, exist_ok=True)
    categories = ("Updates", "Announcements", "Events", "Notice")
    items = []
    for index in range(8):
        path = assets / f"news-{index}.png"
        placeholder_art(320, 180, f"news-{index}").save(str(path))
        items.append(
            {
                "id": str(index + 1),
                "title": (
                    f"Official broadcast #{index + 1}: patch notes, events "
                    "and a look at what comes next"
                ),
                "summary": "Full details on the official site.",
                "category": categories[index % len(categories)],
                "published_at": 1_700_000_000_000 + index * 86_400_000,
                "image": None,
                "image_path": str(path),
                "url": f"https://example.invalid/news/{index + 1}",
            }
        )
    return NewsResult(items=items)


def _fake_media(workdir: Path) -> tuple[str, list[dict]]:
    """Local placeholder hero + banners so the preview never needs network."""
    from nadare.gui.theme import placeholder_art

    assets = workdir / "media"
    assets.mkdir(parents=True, exist_ok=True)
    hero_path = assets / "hero.png"
    placeholder_art(1280, 520, "official-hero").save(str(hero_path))
    banners: list[dict] = []
    for index in range(3):
        path = assets / f"banner-{index}.png"
        placeholder_art(680, 240, f"banner-{index}").save(str(path))
        banners.append(
            {"image_path": str(path), "url": f"https://example.invalid/events/{index + 1}"}
        )
    return str(hero_path), banners


def _fake_config(workdir: Path, *, populate: bool, config=None):
    from nadare.core.config import Config

    if config is None:
        config = Config.load(workdir / "config.json")
    if populate:
        for name, title, backend in PROFILES:
            config.add_profile(
                name,
                backend=backend,
                title=title,
                prefix=str(workdir / "prefixes" / name),
                install_dir=str(workdir / "games" / name),
                exe_name="game.exe",
            )
    return config


def main() -> int:
    from PyQt6.QtWidgets import QApplication, QWidget

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([])

    with tempfile.TemporaryDirectory(prefix="nadare-theme-preview-") as tmp:
        workdir = Path(tmp)
        for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            os.environ[var] = str(workdir / var.lower())
        os.environ["HOME"] = str(workdir / "home")
        os.environ["NADARE_NO_KEYRING"] = "1"
        (workdir / "home").mkdir(parents=True, exist_ok=True)

        from nadare.gui.main_window import MainWindow

        # Construct before adding profiles so the constructor's status probe is a
        # no-op; the preview must never touch the network.
        config = _fake_config(workdir, populate=False)
        window = MainWindow(config)
        window._start = lambda *args, **kwargs: None  # type: ignore[method-assign]
        _fake_config(workdir, populate=True, config=config)
        window.refresh_library()

        # Fake statuses: three installed games (one running, one updatable) and
        # one not-installed profile that must stay out of the sidebar.
        from nadare.core.backends import Status

        window._on_result("neon-drift", "status", Status("running", running=True))
        window._on_result("aether-arc", "status", Status("installed"))
        window._on_result(
            "midnight-run", "status", Status("installed", data={"update_available": True})
        )
        window._on_result("dust-veil", "status", Status("not_installed"))

        window.resize(1180, 780)
        window.show()
        app.processEvents()
        library_path = OUTPUT_DIR / "library.png"
        assert window.grab().save(str(library_path)), "failed to write library.png"

        sidebar = window.findChild(QWidget, "Sidebar")
        assert sidebar is not None, "sidebar widget missing"
        window.sidebar_games.set_active("neon-drift")
        app.processEvents()
        sidebar_path = OUTPUT_DIR / "sidebar.png"
        assert sidebar.grab().save(str(sidebar_path)), "failed to write sidebar.png"

        window.open_game(PROFILES[0][0])
        app.processEvents()
        game_path = OUTPUT_DIR / "game.png"
        assert window.grab().save(str(game_path)), "failed to write game.png"

        hero_path, banners = _fake_media(workdir)
        window.game.set_media(hero_path, banners)
        window.game.set_news(_fake_news(workdir))
        window.game.wait_for_images()
        app.processEvents()
        hero_shot = OUTPUT_DIR / "game-hero.png"
        assert window.grab().save(str(hero_shot)), "failed to write game-hero.png"

        window.game.set_news(_fake_news(workdir))
        window.game.wait_for_images()
        app.processEvents()
        news_path = OUTPUT_DIR / "game-news.png"
        assert window.grab().save(str(news_path)), "failed to write game-news.png"

        # Settings page (per-backend Game options card).
        window.open_settings()
        app.processEvents()
        settings_path = OUTPUT_DIR / "settings.png"
        assert window.grab().save(str(settings_path)), "failed to write settings.png"

        # Add game wizard.
        from nadare.gui.add_game import AddGameDialog

        dialog = AddGameDialog(config, window)
        dialog.set_backend("stellasora-yostar")
        dialog.resize(600, 640)
        dialog.show()
        app.processEvents()
        add_game_path = OUTPUT_DIR / "add-game.png"
        assert dialog.grab().save(str(add_game_path)), "failed to write add-game.png"
        dialog.close()
        dialog.deleteLater()
        app.processEvents()

        window.close()
        window.deleteLater()
        app.processEvents()

    print(library_path)
    print(sidebar_path)
    print(game_path)
    print(hero_shot)
    print(news_path)
    print(settings_path)
    print(add_game_path)
    return 0


def library_preview() -> int:
    """Render the compact library and neighbouring pages using only local fixtures."""
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    from PyQt6.QtCore import QEventLoop
    from PyQt6.QtWidgets import QApplication

    from nadare.core.backends import Status
    from nadare.gui.main_window import MainWindow
    from nadare.gui.theme import placeholder_art

    output = OUTPUT_DIR
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    # Keep fixture writes in the worktree; only the requested PNGs go to output.
    with tempfile.TemporaryDirectory(prefix=".library-preview-", dir=REPO_ROOT) as tmp:
        workdir = Path(tmp)
        for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            os.environ[var] = str(workdir / var.lower())
        os.environ["NADARE_NO_KEYRING"] = "1"
        os.environ["NADARE_INDEX_BASE"] = ""
        os.environ["NADARE_RELEASES_API"] = ""
        os.environ["NADARE_ASSUME_NOTICES"] = "1"
        from nadare.core.config import Config

        config = Config.load(workdir / "config.json")
        art_root = Path(os.environ.get(
            "NADARE_PREVIEW_ART",
            "/mnt/kikyou/work/visual-refresh-2026-11-13/proto-art/nadare/covers",
        ))

        def local_art(profile, title=None, kind="cover", **kwargs):
            from nadare.core import artwork
            slug = artwork.slug(profile, title)
            slot = "cover-wide" if kwargs.get("art") == "wide" else "cover"
            hit = next((p for p in art_root.glob(f"{slug}-{slot}.*") if p.suffix != ".src"), None)
            return hit or profile.get("cover")

        # Disable startup workers before constructing the window: this is a local render.
        with (
            patch("nadare.core.artwork.fetch", side_effect=local_art),
            patch.object(MainWindow, "_start", return_value=False),
            patch.object(MainWindow, "_start_update_check"),
            patch.object(MainWindow, "_start_notices_check"),
            patch.object(MainWindow, "_start_steam_shortcut_cleanup"),
            patch.object(MainWindow, "_start_found_copies_check"),
        ):
            window = MainWindow(config)
            games = [
                ("wuthering-waves", "Wuthering Waves"),
                ("girls-frontline-2", "Girls' Frontline 2"),
                ("zenless-zone-zero", "Zenless Zone Zero"),
                ("punishing-gray-raven", "Punishing: Gray Raven"),
                ("arknights", "Arknights"),
                ("genshin-impact", "Genshin Impact"),
                ("stella-sora", "Stella Sora"),
                ("honkai-impact-3rd", "Honkai Impact 3rd"),
                ("honkai-star-rail", "Honkai: Star Rail"),
                ("neverness-to-everness", "Neverness to Everness"),
                ("goddess-of-victory-nikke", "Goddess of Victory: NIKKE"),
                ("brown-dust-2", "Brown Dust 2"),
                ("aether-gazer", "Aether Gazer"),
                ("arknights-endfield", "Arknights: Endfield"),
            ]
            for index, (name, title) in enumerate(games):
                candidates = list(art_root.glob(f"{name}-cover.*"))
                cover = next((p for p in candidates if p.suffix != ".src"), None)
                if cover is None:
                    cover = workdir / f"{name}.png"
                    placeholder_art(400, 520, title).save(str(cover))
                wide = next(
                    (p for p in art_root.glob(f"{name}-cover-wide.*") if p.suffix != ".src"), cover
                )
                now = datetime.now(UTC)
                config.add_profile(
                    name,
                    title=title,
                    backend="wine",
                    cover=str(cover),
                    wide=str(wide),
                    favourite=index < 3,
                    last_played=(now - timedelta(hours=index + 2)).isoformat() if index < 3 else "",
                    added_at=(now - timedelta(days=3 if index in (4, 6) else 30)).isoformat(),
                )
            window.refresh_library()
            for index, (name, _) in enumerate(games):
                status = (
                    Status("not_installed")
                    if index == 6
                    else Status("installed", data={"update_available": True})
                    if index == 4
                    else Status("running")
                    if index == 3
                    else Status("installed")
                )
                window._snapshot(name)["status"] = status
                window.library.set_state(name, status)
                window.sidebar_games.set_status(name, status)
            window.show_library()
            window.show()
            window.library.search.setFocus()
            for width, height in ((1280, 800), (1920, 1080)):
                window.resize(width, height)
                for _ in range(8):
                    app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
                window.library.wait_for_images()
                app.processEvents()
                path = output / f"library-{width}x{height}.png"
                assert window.grab().save(str(path))
                print(path)
            window.resize(1280, 800)
            for name, show in (
                ("store", window.show_store),
                ("game", lambda: window.open_game(games[0][0])),
                ("settings", window.open_settings),
            ):
                show()
                app.processEvents()
                if name == "store":
                    window.store.grid.wait_for_images()
                elif name == "game":
                    window.game.wait_for_images()
                app.processEvents()
                path = output / f"{name}-1280x800.png"
                assert window.grab().save(str(path))
                print(path)
            window.close()
            window.deleteLater()
            app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(library_preview() if "--library" in sys.argv else main())
