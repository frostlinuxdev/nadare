#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Developer convenience wrapper.
#
#   scripts/dev.sh setup     # create .venv and install runtime + dev deps
#   scripts/dev.sh test      # pytest
#   scripts/dev.sh lint      # ruff check
#   scripts/dev.sh fmt       # ruff format
#   scripts/dev.sh gui       # run the PyQt6 GUI from source
#   scripts/dev.sh cli ...   # run the CLI, e.g. `scripts/dev.sh cli list`
#   scripts/dev.sh stub      # build the CZN PCSDK stub (mingw-w64)
#   scripts/dev.sh build     # PyInstaller onedir bundle
#   scripts/dev.sh clean     # remove caches and build output
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

PY=".venv/bin/python"
PIP=".venv/bin/pip"

ensure_venv() {
    [[ -x "$PY" ]] && return 0
    # Prefer a real Python 3.12/3.13: the system 3.14 ships a broken PyQt6 stub.
    local py="${NADARE_PYTHON:-}"
    if [[ -z "$py" ]] && command -v pyenv >/dev/null 2>&1; then
        py="$(pyenv which python3.12 2>/dev/null || pyenv which python3.13 2>/dev/null || true)"
    fi
    [[ -z "$py" ]] && py="python3"
    echo "[*] Creating .venv with $py"
    if command -v uv >/dev/null 2>&1; then
        uv venv --python "$py" .venv
    else
        "$py" -m venv .venv
    fi
}

case "${1:-help}" in
    setup)
        ensure_venv
        if command -v uv >/dev/null 2>&1 && [[ -f uv.lock ]]; then
            # Reproduce the committed lock exactly instead of resolving the
            # direct deps (and their transitive closure) freshly via pip.
            echo "[*] Syncing the locked environment (uv sync --frozen --extra dev)"
            uv sync --frozen --extra dev
        else
            echo "[*] uv/uv.lock unavailable: falling back to pip + requirements.txt"
            "$PIP" install --upgrade pip
            "$PIP" install -r requirements.txt
            "$PIP" install -e ".[dev]"
        fi
        echo "[+] Ready. Try: scripts/dev.sh cli list"
        ;;
    test)
        ensure_venv
        exec "$PY" -m pytest "${@:2}"
        ;;
    lint)
        ensure_venv
        exec "$PY" -m ruff check "${@:2}" src tests packaging scripts ci
        ;;
    fmt)
        ensure_venv
        exec "$PY" -m ruff format "${@:2}" src tests packaging
        ;;
    gui)
        ensure_venv
        exec "$PY" -c 'from nadare.gui import run; raise SystemExit(run())'
        ;;
    cli)
        ensure_venv
        exec "$PY" -m nadare "${@:2}"
        ;;
    stub)
        exec "$root/scripts/build_stub.sh" "${@:2}"
        ;;
    build)
        ensure_venv
        exec "$PY" -m PyInstaller --clean -y packaging/nadare.spec
        ;;
    clean)
        rm -rf build dist .pytest_cache .ruff_cache .mypy_cache
        find . -type d -name __pycache__ -prune -exec rm -rf {} +
        echo "[+] Cleaned"
        ;;
    help|*)
        cat <<'EOF'
Developer convenience wrapper.

  scripts/dev.sh setup     # create .venv and install runtime + dev deps
  scripts/dev.sh test      # pytest
  scripts/dev.sh lint      # ruff check
  scripts/dev.sh fmt       # ruff format
  scripts/dev.sh gui       # run the PyQt6 GUI from source
  scripts/dev.sh cli ...   # run the CLI, e.g. `scripts/dev.sh cli list`
  scripts/dev.sh stub      # build the CZN PCSDK stub (mingw-w64)
  scripts/dev.sh build     # PyInstaller onedir bundle
  scripts/dev.sh clean     # remove caches and build output
EOF
        ;;
esac
