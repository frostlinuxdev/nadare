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

        window._on_result("neon-drift", Status("running", running=True))
        window._on_result("aether-arc", Status("installed"))
        window._on_result(
            "midnight-run", Status("installed", data={"update_available": True})
        )
        window._on_result("dust-veil", Status("not_installed"))

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


if __name__ == "__main__":
    raise SystemExit(main())
