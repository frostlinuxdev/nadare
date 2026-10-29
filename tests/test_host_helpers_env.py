# SPDX-License-Identifier: GPL-3.0-or-later
"""Host helpers and unknown binaries must not inherit launcher secrets.

Game/installer children already go through :func:`nadare.core.process.child_environ`.
The remaining helper-tool calls (7-Zip, the endfield shim compiler, Flatpak host
tools, Vulkan probes, ...) need the same filtered environment.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path

import pytest

from nadare.core import sevenzip

SRC = Path(__file__).resolve().parents[1] / "src" / "nadare"
_SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call", "call"}
_OS_EXEC_PREFIXES = ("exec", "system")

#: ``(path relative to src/nadare, function)`` -> one-line reason. Keep this
#: short: a helper that never sees secrets should still get ``env=``, not an
#: allowlist entry.
ALLOWLIST: dict[tuple[str, str], str] = {}


class _CallGuard(ast.NodeVisitor):
    """Flag child-spawning calls that do not pass an explicit ``env=``."""

    def __init__(self, relative: str, tree: ast.AST | None = None) -> None:
        self.relative = relative
        self.functions: list[str] = []
        self.aliases: list[set[str]] = [set()]
        self.violations: list[str] = []
        #: names bound to the ``subprocess`` module (``import subprocess as sp``) and to its
        #: spawning functions (``from subprocess import run as go``), anywhere in the file.
        self.module_names: set[str] = {"subprocess"}
        self.func_names: set[str] = set()
        for node in ast.walk(tree) if tree is not None else ():
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.name == "subprocess":
                        self.module_names.add(item.asname or "subprocess")
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                for item in node.names:
                    if item.name in _SUBPROCESS_FUNCS:
                        self.func_names.add(item.asname or item.name)

    def _is_subprocess_ref(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in self.module_names
            and node.attr in _SUBPROCESS_FUNCS
        )

    def _is_os_spawn_ref(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr.startswith(_OS_EXEC_PREFIXES)
        )

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        positional = [*node.args.posonlyargs, *node.args.args]
        defaults: dict[str, ast.expr] = {}
        for arg, default in zip(
            positional[len(positional) - len(node.args.defaults) :],
            node.args.defaults,
            strict=False,
        ):
            defaults[arg.arg] = default
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=False):
            if default is not None:
                defaults[arg.arg] = default
        aliases = {
            arg.arg
            for arg in args
            if arg.arg in defaults and self._is_subprocess_ref(defaults[arg.arg])
        }
        self.functions.append(node.name)
        self.aliases.append(aliases)
        self.generic_visit(node)
        self.aliases.pop()
        self.functions.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        refs = [*ast.walk(node.func)]
        is_spawn = any(self._is_subprocess_ref(ref) or self._is_os_spawn_ref(ref) for ref in refs)
        if isinstance(node.func, ast.Name) and (
            node.func.id in self.func_names or any(node.func.id in names for names in self.aliases)
        ):
            is_spawn = True
        if is_spawn and not any(keyword.arg == "env" for keyword in node.keywords):
            function = ".".join(self.functions) or "<module>"
            if (self.relative, function) not in ALLOWLIST:
                self.violations.append(
                    f"{self.relative}:{node.lineno} in {function} spawns a child without env="
                )
        self.generic_visit(node)


def _unfiltered_calls() -> list[str]:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guard = _CallGuard(str(path.relative_to(SRC)), tree)
        guard.visit(tree)
        violations += guard.violations
    return violations


def test_helper_spawns_pass_a_filtered_environment() -> None:
    assert _unfiltered_calls() == []


def test_allowlist_entries_have_a_reason() -> None:
    for key, reason in ALLOWLIST.items():
        assert len(key) == 2
        assert reason.strip()


def test_sevenzip_probe_does_not_see_launcher_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "7zzs"
    data = b"stand-in for 7zzs"
    binary.write_bytes(data)
    binary.chmod(0o755)
    digest = hashlib.sha256(data).hexdigest()
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("NADARE_GITHUB_TOKEN", "ghp-launcher")
    monkeypatch.setenv("SGDB_API_KEY", "steamgriddb-key")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(subprocess, "run", fake_run)

    # The default parameter binds subprocess.run at import time, so pass the
    # monkeypatched entry point explicitly to observe the real call path.
    found = sevenzip.find(
        which=lambda _: None,
        bundled=binary,
        expected_sha256=digest,
        cache_dir=lambda: tmp_path / "cache",
        run=subprocess.run,
    )

    assert found == str(binary)
    env = captured["env"]
    assert isinstance(env, dict)
    assert "NADARE_GITHUB_TOKEN" not in env
    assert "SGDB_API_KEY" not in env
    assert env["PATH"] == "/usr/bin:/bin"


def test_endfield_shim_compiler_does_not_see_launcher_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nadare.core import endfield_shim

    source = tmp_path / "shim.c"
    source.write_text("int main(void) { return 0; }\n")
    out = tmp_path / "suspend_start.exe"
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        Path(argv[argv.index("-o") + 1]).write_bytes(b"MZ")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("NADARE_GITHUB_TOKEN", "ghp-launcher")
    monkeypatch.setenv("SGDB_API_KEY", "steamgriddb-key")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(subprocess, "run", fake_run)

    # As above: the default parameter bound subprocess.run at import time.
    built = endfield_shim.build_shim(
        out,
        source=source,
        cc="fake-gcc",
        which=lambda name: "/usr/bin/" + name,
        run=subprocess.run,
    )

    assert built == out
    env = captured["env"]
    assert isinstance(env, dict)
    assert "NADARE_GITHUB_TOKEN" not in env
    assert "SGDB_API_KEY" not in env
    assert env["PATH"] == "/usr/bin:/bin"


def _violations_in(source: str) -> list[str]:
    tree = ast.parse(source)
    guard = _CallGuard("synthetic.py", tree)
    guard.visit(tree)
    return guard.violations


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess as sp\nsp.run(['x'])\n",
        "from subprocess import run\nrun(['x'])\n",
        "from subprocess import Popen as P\nP(['x'])\n",
        "import subprocess\nsubprocess.check_output(['x'])\n",
    ],
)
def test_guard_catches_aliased_spawns(source: str) -> None:
    assert _violations_in(source), source


def test_guard_accepts_a_call_that_passes_env() -> None:
    assert _violations_in("import subprocess as sp\nsp.run(['x'], env={})\n") == []
