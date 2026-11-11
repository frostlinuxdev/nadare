# SPDX-License-Identifier: GPL-3.0-or-later
"""#974 item 3: the managed umu is validated before anyone can see it.

``ensure_managed`` published ``umu-run`` and only then ran it once to check it. ``managed()``
trusts any executable file there, so a second caller could be handed an unchecked copy, and a
failed check then deleted the file that caller was using.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_umu import _Completed, _fake_download, _only_python, _payload

from nadare.core import umu


def test_the_check_runs_on_a_private_copy_before_publishing(monkeypatch):
    _fake_download(monkeypatch, _payload())
    _only_python(monkeypatch)
    seen: list[tuple[str, bool]] = []

    def run(argv, **kwargs):
        checked = Path(argv[1])
        seen.append((str(checked), umu.managed_path().exists()))
        return _Completed()

    monkeypatch.setattr(umu.subprocess, "run", run)
    dest = umu.ensure_managed()
    assert dest == umu.managed_path() and dest.is_file()
    (checked, published_during_check), = seen
    assert checked != str(dest)  # a private candidate, not the published path
    assert published_during_check is False
    assert not Path(checked).exists()  # the candidate became the published file
    assert sorted(p.name for p in dest.parent.iterdir()) == ["umu-run"]


def test_a_failed_check_publishes_nothing_and_leaves_no_candidate(monkeypatch):
    _fake_download(monkeypatch, _payload())
    _only_python(monkeypatch)
    monkeypatch.setattr(
        umu.subprocess, "run", lambda argv, **k: type("R", (), {"returncode": 1})()
    )
    with pytest.raises(umu.UmuError):
        umu.ensure_managed()
    dest = umu.managed_path()
    assert not dest.exists()
    assert not dest.parent.exists() or list(dest.parent.iterdir()) == []


def test_a_failed_check_never_deletes_a_copy_another_process_published(monkeypatch):
    _fake_download(monkeypatch, _payload())
    _only_python(monkeypatch)
    dest = umu.managed_path()

    def run(argv, **kwargs):
        # Meanwhile another Nadare finished its own install of a good copy.
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("#!/usr/bin/python3\nprint('other')\n")
        dest.chmod(0o755)
        return type("R", (), {"returncode": 1})()

    monkeypatch.setattr(umu.subprocess, "run", run)
    with pytest.raises(umu.UmuError):
        umu.ensure_managed()
    assert dest.read_text() == "#!/usr/bin/python3\nprint('other')\n"
