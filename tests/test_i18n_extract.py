# SPDX-License-Identifier: GPL-3.0-or-later
"""#1602 item 1: the catalogue extractor reports missing and unused keys.

``scripts/i18n_extract.py`` scans the source for keys passed to the existing
translate calls and compares them with ``en.json``. It never edits the
catalogue; ``--check`` is the CI gate for keys used in code but not shipped.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "i18n_extract.py"


@pytest.fixture(scope="module")
def extractor():
    spec = importlib.util.spec_from_file_location("i18n_extract_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path):
    src = tmp_path / "src"
    _write(
        src / "pkg" / "commands.py",
        "from nadare.core import i18n\n"
        "\n"
        "def run():\n"
        "    print(i18n.translate('used.key'))\n"
        "    print(i18n.translate('missing.key'))\n"
        "    return i18n.translate(name)\n",
    )
    _write(
        src / "pkg" / "window.py",
        "from nadare.gui.i18n import tr\n"
        "\n"
        "def title():\n"
        "    return tr('direct.used') + tr('tr.missing')\n",
    )
    catalogue = tmp_path / "en.json"
    catalogue.write_text(
        json.dumps(
            {
                "used.key": "Used",
                "direct.used": "Direct",
                "unused.key": "Unused",
            }
        ),
        encoding="utf-8",
    )
    return src, catalogue


def test_extractor_finds_a_missing_key(extractor, tree) -> None:
    src, catalogue = tree
    missing, _unused = extractor.compare(src, catalogue)
    assert missing == ["missing.key", "tr.missing"]


def test_extractor_finds_an_unused_key(extractor, tree) -> None:
    src, catalogue = tree
    _missing, unused = extractor.compare(src, catalogue)
    assert unused == ["unused.key"]


def test_extractor_ignores_dynamic_keys(extractor, tree) -> None:
    src, _catalogue = tree
    used = extractor.used_keys(src)
    assert "used.key" in used
    assert "name" not in used  # the non-literal first argument is not a key


def test_check_exits_one_when_a_key_is_missing(tree) -> None:
    src, catalogue = tree
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--src",
            str(src),
            "--catalogue",
            str(catalogue),
            "--check",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "missing.key" in result.stdout


def test_check_passes_when_only_unused_keys_remain(extractor, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write(src / "a.py", "from nadare.core import i18n\ni18n.translate('used.key')\n")
    catalogue = tmp_path / "en.json"
    catalogue.write_text(
        json.dumps({"used.key": "Used", "unused.key": "Unused"}), encoding="utf-8"
    )
    assert extractor.main(["--src", str(src), "--catalogue", str(catalogue), "--check"]) == 0


def test_extractor_never_rewrites_the_catalogue(tree) -> None:
    src, catalogue = tree
    before = catalogue.read_bytes()
    subprocess.run(
        [sys.executable, str(SCRIPT), "--src", str(src), "--catalogue", str(catalogue), "--check"],
        capture_output=True,
        text=True,
    )
    assert catalogue.read_bytes() == before


def test_the_real_tree_passes_check(extractor) -> None:
    assert extractor.main(["--check"]) == 0


@pytest.mark.parametrize(
    "source",
    [
        "import nadare.core.i18n\nnadare.core.i18n.translate('missing')\n",
        "from nadare.core import i18n\ni18n._('missing')\n",
        "from nadare.core.i18n import translate\ntranslate(key='missing')\n",
        "from nadare.gui.i18n import tr\ntr(text='missing')\n",
    ],
)
def test_extractor_recognises_literal_call_forms(extractor, tmp_path, source):
    _write(tmp_path / "calls.py", source)
    assert extractor.used_keys(tmp_path) == {"missing"}


@pytest.mark.parametrize(
    "source",
    [
        "from nadare.gui.i18n import tr_n\ntr_n('{n} games', 2)\n",
        "from nadare.core.i18n import translate_plural\ntranslate_plural('{n} games', 2)\n",
        "import nadare.core.i18n\nnadare.core.i18n._n('{n} games', 2)\n",
    ],
)
def test_extractor_finds_a_plural_key(extractor, tmp_path, source):
    _write(tmp_path / "calls.py", source)
    assert extractor.used_keys(tmp_path) == {"{n} games"}
    assert extractor.plural_keys(tmp_path) == {"{n} games"}


def test_check_fails_when_a_plural_key_is_a_plain_string(extractor, tmp_path) -> None:
    src = tmp_path / "src"
    _write(
        src / "a.py",
        "from nadare.gui.i18n import tr_n\n"
        "tr_n('{n} games', 2)\n",
    )
    catalogue = tmp_path / "en.json"
    catalogue.write_text(json.dumps({"{n} games": "{n} games"}), encoding="utf-8")
    assert extractor.type_mismatches(src, catalogue) == [
        "plural call on a plain string: {n} games"
    ]
    assert extractor.main(["--src", str(src), "--catalogue", str(catalogue), "--check"]) == 1


def test_check_fails_when_a_plain_key_has_no_other_form(extractor, tmp_path) -> None:
    src = tmp_path / "src"
    _write(
        src / "a.py",
        "from nadare.gui.i18n import tr\n"
        "tr('items')\n",
    )
    catalogue = tmp_path / "en.json"
    catalogue.write_text(json.dumps({"items": {"one": "{n} item"}}), encoding="utf-8")
    assert extractor.type_mismatches(src, catalogue) == [
        'plain call on a key with no "other" form: items'
    ]
    assert extractor.main(["--src", str(src), "--catalogue", str(catalogue), "--check"]) == 1


def test_check_accepts_a_plural_object_and_a_plain_other(extractor, tmp_path) -> None:
    src = tmp_path / "src"
    _write(
        src / "a.py",
        "from nadare.gui.i18n import tr, tr_n\n"
        "tr_n('{n} games', 2)\n"
        "tr('items')\n",
    )
    catalogue = tmp_path / "en.json"
    catalogue.write_text(
        json.dumps(
            {
                "{n} games": {"one": "{n} game", "other": "{n} games"},
                "items": {"other": "{n} items"},
            }
        ),
        encoding="utf-8",
    )
    assert extractor.type_mismatches(src, catalogue) == []
    assert extractor.main(["--src", str(src), "--catalogue", str(catalogue), "--check"]) == 0


def test_check_reports_a_plural_type_mismatch_even_without_check(
    extractor, tmp_path, capsys
) -> None:
    src = tmp_path / "src"
    _write(src / "a.py", "from nadare.gui.i18n import tr_n\ntr_n('{n} games', 2)\n")
    catalogue = tmp_path / "en.json"
    catalogue.write_text(json.dumps({"{n} games": "{n} games"}), encoding="utf-8")
    assert extractor.main(["--src", str(src), "--catalogue", str(catalogue)]) == 0
    assert "plural call on a plain string" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["missing-tree", "syntax", "encoding", "unreadable"])
def test_check_rejects_incomplete_scan(extractor, tmp_path, capsys, monkeypatch, failure):
    src = tmp_path / "source with spaces"
    catalogue = tmp_path / "en.json"
    catalogue.write_text("{}", encoding="utf-8")
    if failure != "missing-tree":
        path = src / "broken.py"
        _write(path, "def invalid(:" if failure == "syntax" else "pass")
        if failure == "encoding":
            path.write_bytes(b"\xff")
        if failure == "unreadable":
            original = Path.read_text

            def read(self, *args, **kwargs):
                if self == path:
                    raise PermissionError("cannot read source")
                return original(self, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", read)
    assert extractor.main(["--src", str(src), "--catalogue", str(catalogue), "--check"]) == 2
    assert str(src) in capsys.readouterr().err


# --- #1602 part 2: tr(CONSTANT) and the "not checkable" report -----------------


def test_a_module_constant_key_is_resolved(extractor, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write(
        src / "panel.py",
        "from nadare.gui.i18n import tr\n"
        "NOTE = (\n"
        '    "shader."\n'
        '    "note"\n'
        ")\n"
        "print(tr(NOTE))\n",
    )
    assert extractor.used_keys(src) == {"shader.note"}


def test_a_module_constant_key_is_checked_against_the_catalogue(extractor, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write(
        src / "panel.py",
        'from nadare.gui.i18n import tr\nNOTE = "shader.note"\nprint(tr(NOTE))\n',
    )
    catalogue = tmp_path / "en.json"
    catalogue.write_text(json.dumps({"other.key": "Other"}), encoding="utf-8")
    missing, _unused = extractor.compare(src, catalogue)
    assert missing == ["shader.note"]
    assert extractor.main(["--src", str(src), "--catalogue", str(catalogue), "--check"]) == 1
    catalogue.write_text(json.dumps({"shader.note": "Shader note"}), encoding="utf-8")
    assert extractor.main(["--src", str(src), "--catalogue", str(catalogue), "--check"]) == 0


def test_an_annotated_module_constant_is_resolved(extractor, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write(
        src / "panel.py",
        "from typing import Final\n"
        "from nadare.gui.i18n import tr\n"
        'NOTE: Final = "shader.note"\n'
        "print(tr(NOTE))\n",
    )
    assert extractor.used_keys(src) == {"shader.note"}


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ('NAME = "one"\nNAME = "two"\ntr(NAME)\n', 4),
        ("from other import NAME\ntr(NAME)\n", 3),
        ("tr(config.NAME)\n", 2),
        ('tr(f"{prefix} key")\n', 2),
        ("tr(build_key())\n", 2),
        ('tr("constant" + suffix)\n', 2),
    ],
)
def test_a_non_literal_key_is_reported_not_checked(
    extractor, tmp_path: Path, source: str, line: int
) -> None:
    src = tmp_path / "src"
    _write(src / "panel.py", "from nadare.gui.i18n import tr\n" + source)
    keys, uncheckable = extractor.scan(src)
    assert keys == set()
    assert [(item.path.name, item.line) for item in uncheckable] == [("panel.py", line)]


def test_the_dynamic_marker_skips_a_non_literal_key(extractor, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write(
        src / "panel.py",
        'from nadare.gui.i18n import tr\nname = input()\ntr(name)  # i18n: dynamic\n',
    )
    keys, uncheckable = extractor.scan(src)
    assert keys == set()
    assert uncheckable == []


def test_check_fails_and_names_each_uncheckable_call(extractor, tmp_path: Path, capsys) -> None:
    src = tmp_path / "src"
    _write(
        src / "panel.py",
        'from nadare.gui.i18n import tr\nname = input()\ntr(name)\ntr(name)  # i18n: dynamic\n',
    )
    catalogue = tmp_path / "en.json"
    catalogue.write_text("{}", encoding="utf-8")
    assert extractor.main(["--src", str(src), "--catalogue", str(catalogue), "--check"]) == 1
    out = capsys.readouterr().out
    assert "not checkable: " in out
    assert "panel.py:3" in out
    assert "panel.py:4" not in out


def test_a_local_name_shadowing_the_constant_is_reported(extractor, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write(
        src / "panel.py",
        "from nadare.gui.i18n import tr\n"
        'NOTE = "shader.note"\n'
        "def show(NOTE):\n"
        "    return tr(NOTE)\n",
    )
    keys, uncheckable = extractor.scan(src)
    assert keys == set()
    assert [(item.path.name, item.line) for item in uncheckable] == [("panel.py", 4)]


@pytest.mark.parametrize("helper", ["translate_plural", "tr_n", "_n"])
@pytest.mark.parametrize("module_call", [False, True])
def test_plural_module_constant_is_checked(extractor, tmp_path, helper, module_call):
    src = tmp_path / "src"
    binding = (
        "from nadare.core import i18n\n"
        if module_call else f"from nadare.core.i18n import {helper} as plural\n"
    )
    call = f"i18n.{helper}" if module_call else "plural"
    _write(src / "panel.py", binding + f'KEY = "items"\n{call}(KEY, 2)\n')
    catalogue = tmp_path / "en.json"
    catalogue.write_text("{}", encoding="utf-8")
    assert extractor.used_keys(src) == {"items"}
    assert extractor.plural_keys(src) == {"items"}
    args = ["--src", str(src), "--catalogue", str(catalogue), "--check"]
    assert extractor.main(args) == 1
    catalogue.write_text('{"items": "plain"}', encoding="utf-8")
    assert extractor.main(args) == 1
    catalogue.write_text('{"items": {"one": "one"}}', encoding="utf-8")
    assert extractor.main(args) == 1
    catalogue.write_text('{"items": {"other": "many"}}', encoding="utf-8")
    assert extractor.main(args) == 0


@pytest.mark.parametrize("helper", ["translate_plural", "tr_n", "_n"])
@pytest.mark.parametrize("module_call", [False, True])
def test_plural_non_literal_is_not_checkable(extractor, tmp_path, capsys, helper, module_call):
    src = tmp_path / "src"
    binding = (
        "from nadare.core import i18n\n"
        if module_call else f"from nadare.core.i18n import {helper} as plural\n"
    )
    call = f"i18n.{helper}" if module_call else "plural"
    source = binding + f'KEY = "items"\ndef show(KEY):\n    return {call}(KEY, 2)'
    path = src / "panel.py"
    _write(path, source + "\n")
    catalogue = tmp_path / "en.json"
    catalogue.write_text("{}", encoding="utf-8")
    args = ["--src", str(src), "--catalogue", str(catalogue), "--check"]
    assert extractor.main(args) == 1
    assert f"not checkable: {path}:4" in capsys.readouterr().out
    assert extractor.used_keys(src) == set()
    _write(path, source + "  # i18n: dynamic\n")
    assert extractor.main(args) == 0
    assert extractor.scan(src) == (set(), [])
