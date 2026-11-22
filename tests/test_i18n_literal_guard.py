# SPDX-License-Identifier: GPL-3.0-or-later
"""#1602 part 2: the ratchet against new untranslated GUI strings.

``scripts/i18n_literals.py`` counts string literals passed straight to the Qt
calls that show text and compares each file with the committed baseline. A file
may shrink, never grow; a new file with literals fails until it is translated.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "i18n_literals.py"


def _load():
    spec = importlib.util.spec_from_file_location("i18n_literals", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(tmp_path: Path, body: str, name: str = "window.py") -> Path:
    _write(tmp_path / "src" / "nadare" / "gui" / name, body)
    return tmp_path


def _baseline(tmp_path: Path, counts: dict[str, int]) -> Path:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(counts), encoding="utf-8")
    return path


MIXED = """\
from PyQt6.QtWidgets import QLabel, QMessageBox

QLabel("Visible")
QLabel("")
QLabel("123")
QLabel(tr("Done"))
label.setText("Also visible")
label.setToolTip("")
label.setText(f"{count} items")
QMessageBox.information(self, "Title", "Body")
"""


def test_it_counts_only_user_visible_literals(tmp_path: Path) -> None:
    tree = _tree(tmp_path, MIXED)
    found = guard.literals(tree)
    assert len(found["src/nadare/gui/window.py"]) == 4
    assert {line for line, _text in found["src/nadare/gui/window.py"]} == {3, 7, 10}


def test_a_new_literal_over_the_baseline_fails_naming_file_and_line(tmp_path: Path) -> None:
    tree = _tree(tmp_path, MIXED)
    baseline = _baseline(tmp_path, {"src/nadare/gui/window.py": 3})
    problems = guard.check(guard.literals(tree), guard.read_baseline(baseline))
    joined = "\n".join(problems)
    assert "src/nadare/gui/window.py:3" in joined
    assert "tr()" in joined and "en.json" in joined
    assert guard.main(["--root", str(tree), "--baseline", str(baseline)]) == 1


def test_equal_to_the_baseline_passes(tmp_path: Path) -> None:
    tree = _tree(tmp_path, MIXED)
    baseline = _baseline(tmp_path, {"src/nadare/gui/window.py": 4})
    assert guard.check(guard.literals(tree), guard.read_baseline(baseline)) == []
    assert guard.main(["--root", str(tree), "--baseline", str(baseline)]) == 0


def test_fewer_than_the_baseline_passes_with_a_hint(tmp_path: Path, capsys) -> None:
    tree = _tree(tmp_path, MIXED)
    baseline = _baseline(tmp_path, {"src/nadare/gui/window.py": 6})
    assert guard.check(guard.literals(tree), guard.read_baseline(baseline)) == []
    hints = guard.hints(guard.literals(tree), guard.read_baseline(baseline))
    assert len(hints) == 1
    assert "lower the baseline" in hints[0] and "--update-baseline" in hints[0]
    assert guard.main(["--root", str(tree), "--baseline", str(baseline)]) == 0
    assert "lower the baseline" in capsys.readouterr().out


def test_a_new_file_with_literals_fails(tmp_path: Path) -> None:
    tree = _tree(tmp_path, MIXED)
    _write(tree / "src" / "nadare" / "gui" / "new.py", 'QLabel("New text")\n')
    baseline = _baseline(tmp_path, {"src/nadare/gui/window.py": 4})
    problems = guard.check(guard.literals(tree), guard.read_baseline(baseline))
    assert any("new.py" in line and "tr()" in line for line in problems)


def test_check_never_rewrites_the_baseline(tmp_path: Path) -> None:
    tree = _tree(tmp_path, MIXED)
    baseline = _baseline(tmp_path, {"src/nadare/gui/window.py": 1})
    before = baseline.read_bytes()
    guard.main(["--root", str(tree), "--baseline", str(baseline)])
    assert baseline.read_bytes() == before


def test_update_baseline_rewrites_it(tmp_path: Path) -> None:
    tree = _tree(tmp_path, MIXED)
    baseline = _baseline(tmp_path, {"src/nadare/gui/window.py": 1, "src/nadare/gui/gone.py": 9})
    assert guard.main(["--root", str(tree), "--baseline", str(baseline), "--update-baseline"]) == 0
    assert json.loads(baseline.read_text(encoding="utf-8")) == {
        "src/nadare/gui/window.py": 4
    }
    assert guard.main(["--root", str(tree), "--baseline", str(baseline)]) == 0


def test_the_real_tree_passes_its_baseline() -> None:
    current = guard.literals(ROOT)
    baseline = guard.read_baseline(ROOT / guard.BASELINE)
    assert guard.check(current, baseline) == []
    assert baseline
