# SPDX-License-Identifier: GPL-3.0-or-later
"""#1567: the GUI is the product; the CLI is shortcuts, diagnostics and automation.

The commands every desktop shortcut, the release matrix and the upgrade gate run
stay in ``--help``. The rest stay callable, unchanged, but are only named on the
``Advanced:`` line.
"""

from __future__ import annotations

import argparse
import re

import pytest

from nadare import app
from nadare.core import shortcuts

#: Supported in ``--help``: shortcuts, the app's entry point, diagnostics, automation.
KEEP = (
    "launch",
    "gui",
    "doctor",
    "report",
    "status",
    "list",
    "install",
    "update",
    "repair",
    "verify",
    "stop",
)

#: Callable exactly as before, but not part of the supported surface.
ADVANCED = (
    "predownload",
    "remake-prefix",
    "login",
    "fetch-art",
    "shortcut",
    "remove",
    "move-to-flatpak",
    "uninstall-all",
)

#: One minimal argv per advanced command (and per ``shortcut`` action).
ADVANCED_ARGV = (
    ["predownload", "-p", "game"],
    ["remake-prefix", "-p", "game", "--yes"],
    ["login", "-p", "game"],
    ["fetch-art", "-p", "game"],
    ["fetch-art", "--all"],
    ["shortcut", "add", "-p", "game"],
    ["shortcut", "remove", "-p", "game"],
    ["shortcut", "list"],
    ["shortcut", "refresh"],
    ["remove", "-p", "game", "--yes"],
    ["move-to-flatpak", "--yes"],
    ["uninstall-all", "--yes", "--with-games"],
)


def _help() -> str:
    return app.build_parser().format_help()


def _subcommands(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(
        action for action in parser._actions if action.dest == "command" and action.choices
    )


def test_help_lists_the_keep_set_and_hides_the_advanced_commands() -> None:
    text = _help()
    before, marker, after = text.partition("Advanced:")
    assert marker, "the help epilog must name the advanced commands"
    for command in KEEP:
        assert re.search(rf"\b{re.escape(command)}\b", before), command
    for command in ADVANCED:
        assert not re.search(rf"\b{re.escape(command)}\b", before), command
        assert re.search(rf"\b{re.escape(command)}\b", after), command


def test_help_and_advanced_sets_match_the_parser() -> None:
    parser = app.build_parser()
    choices = set(_subcommands(parser).choices)
    assert set(app.KEEP_COMMANDS) == set(KEEP)
    assert set(app.ADVANCED_COMMANDS) == set(ADVANCED)
    assert not set(KEEP) & set(ADVANCED)
    assert choices == set(KEEP) | set(ADVANCED)
    # The backend operations keep the shared resource policy: hiding is help only.
    assert set(app.COMMANDS) <= choices


@pytest.mark.parametrize("command", KEEP)
def test_keep_commands_still_parse(command: str) -> None:
    args = app.build_parser().parse_args([command])
    assert args.command == command


@pytest.mark.parametrize("argv", ADVANCED_ARGV, ids=lambda argv: " ".join(argv))
def test_advanced_commands_are_hidden_but_still_parse(argv: list[str]) -> None:
    args = app.build_parser().parse_args(argv)
    assert args.command == argv[0]


@pytest.mark.parametrize("command", ADVANCED)
def test_advanced_command_help_still_describes_itself(command: str, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        app.build_parser().parse_args([command, "--help"])
    assert exc.value.code == 0
    assert command in capsys.readouterr().out


def test_launch_profile_argument_shape_is_byte_for_byte_stable() -> None:
    """Every desktop and Steam shortcut runs exactly ``launch -p NAME``."""
    parser = app.build_parser()
    args = parser.parse_args(["launch", "-p", "czn"])
    assert (args.command, args.profile) == ("launch", "czn")
    # Nothing else is required, so a shortcut written today keeps working.
    assert args.quiet is False and args.verbose is False

    entry = shortcuts.desktop_entry(
        "czn", "Chaos Zero Nightmare", ["/opt/nadare"], icon="nadare"
    )
    assert "\nExec=/opt/nadare launch -p czn\n" in entry
    assert shortcuts._steam_launch_options("czn", ["/opt/nadare"]) == 'launch -p "czn"'

    # The shortcut's own words parse back to the same command.
    words = ["launch", "-p", 'czn']
    assert parser.parse_args(words).profile == "czn"
