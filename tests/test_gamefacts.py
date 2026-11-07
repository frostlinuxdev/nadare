# SPDX-License-Identifier: GPL-3.0-or-later
"""The distinct facts a game page shows, named once (#943)."""

from __future__ import annotations

import pytest

from nadare.core.gamefacts import InstallFacts, SandboxFacts


@pytest.mark.parametrize(
    ("facts", "pill"),
    [
        (SandboxFacts(supported=False, chosen=True), ""),
        (SandboxFacts(supported=True, chosen=False), ""),
        (SandboxFacts(supported=True, chosen=True), "SANDBOX"),
        # a running game keeps what it started with, whatever the switch says now
        (SandboxFacts(True, True, running=True, active=True), "SANDBOX"),
        (SandboxFacts(True, True, running=True, active=False), "SANDBOX NEXT LAUNCH"),
        (SandboxFacts(True, False, running=True, active=True), "SANDBOX"),
        (SandboxFacts(True, False, running=True, active=False), ""),
        # how it started is not known: fall back to the switch
        (SandboxFacts(True, True, running=True, active=None), "SANDBOX"),
        (SandboxFacts(True, False, running=True, active=None), ""),
    ],
)
def test_the_pill_describes_the_running_process_not_only_the_switch(facts, pill):
    assert facts.pill() == pill


def test_the_switch_is_editable_only_when_nothing_blocks_it_and_the_game_is_stopped():
    assert SandboxFacts(True, False).editable()
    assert not SandboxFacts(True, False, running=True).editable()
    assert not SandboxFacts(True, False, blocked="needs bubblewrap").editable()


def test_the_tooltip_says_why_the_switch_is_locked():
    default = "Hide the home folder"
    assert SandboxFacts(True, False).tooltip(default) == default
    assert "needs bubblewrap" == SandboxFacts(True, False, blocked="needs bubblewrap").tooltip(
        default
    )
    assert "Stop the game" in SandboxFacts(True, True, running=True).tooltip(default)


@pytest.mark.parametrize(
    ("facts", "state"),
    [
        (InstallFacts(installed=False, incomplete=False), "none"),
        (InstallFacts(installed=True, incomplete=False), "complete"),
        (InstallFacts(installed=True, incomplete=True), "interrupted"),
        (InstallFacts(installed=False, incomplete=True), "interrupted"),
        (InstallFacts(installed=True, incomplete=True, paused=True), "paused"),
        (InstallFacts(installed=False, incomplete=False, paused=True), "paused"),
    ],
)
def test_install_state_keeps_never_installed_interrupted_and_paused_apart(facts, state):
    assert facts.state() == state


def test_resume_is_offered_for_an_interrupted_or_paused_install_only():
    assert InstallFacts(True, True).offers_resume()
    assert InstallFacts(False, False, paused=True).offers_resume()
    assert not InstallFacts(True, False).offers_resume()
    assert not InstallFacts(False, False).offers_resume()
