# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# PyInstaller recipe for Nadare.
#
#   Build (from the repo root):
#       .venv/bin/pip install -e ".[build]"
#       .venv/bin/pyinstaller --clean -y packaging/nadare.spec
#
#   Output: dist/nadare/  (a onedir bundle) containing the `nadare` binary,
#   `_internal/` payload and the bundled data files.
#
# Design notes
# ------------
# * **onedir is the default** (EXE + COLLECT). It starts fast, patches cleanly
#   (replace files in place) and is what the .deb / AppImage recipes wrap.
# * **onefile** is possible but not recommended for a launcher: it re-extracts
#   PyQt6 to /tmp on every start, which is slow and can fail on noexec /tmp.
#   For a onefile build, change the tail of this file to:
#
#       exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
#                 name="nadare", debug=False, strip=False, upx=False,
#                 console=True, runtime_tmpdir=None)
#
#   and delete the COLLECT block. See `packaging/nadare_entry.py` for the entry.
# * **console=True** because the same binary is the CLI. On Linux a terminal
#   window is harmless; on Windows packaging you would want two EXEs or a
#   windowed runner. Set console=False only when splitting the CLI out.
# * No icons/game art are shipped: nadare has no copyrighted assets in-repo.
#   Add an `icon=` path here only if you own or licence the artwork.

import os

try:
    from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules
except Exception:  # pragma: no cover - pyinstaller is a build-time dep
    collect_submodules = None
    collect_dynamic_libs = None

# SPECPATH is the directory holding this spec (packaging/).
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
SRC = os.path.join(ROOT, "src")

# Data files that live outside the .py modules and must ship verbatim.
datas = [
    (os.path.join(SRC, "nadare", "assets"), "nadare/assets"),
    (os.path.join(SRC, "nadare", "backends", "czn", "stub_src"),
     "nadare/backends/czn/stub_src"),
    (os.path.join(SRC, "nadare", "backends", "czn", "build_stub.sh"),
     "nadare/backends/czn"),
    # C sources the Endfield launch helper compiles at run time (core/endfield_shim.py).
    (os.path.join(SRC, "nadare", "core", "shim_src"), "nadare/core/shim_src"),
]

# Backends are imported lazily by the registry (core/backends.py _BUILTINS), and
# the CZN backend spawns helpers as `sys.executable -m <module>`, so PyInstaller
# cannot see them. Collect the whole package so a new backend can't be missed
# (arknights/stellasora once were, which broke both Yostar games in the bundle).
hiddenimports = ["nadare.backends"]
if collect_submodules is not None:
    hiddenimports += collect_submodules("nadare.backends")
else:  # pragma: no cover
    raise SystemExit("PyInstaller hooks unavailable: cannot collect nadare.backends")

# An FGO-free bundle (Flathub / no-FGO): drop the backend's hidden imports and
# exclude the package so a missing podman/lepton stack never ships.
excludes = ["tkinter", "unittest", "pydoc", "doctest", "test"]
if os.environ.get("NADARE_WITHOUT_FGO") == "1":
    hiddenimports = [h for h in hiddenimports if not h.startswith("nadare.backends.fgo")]
    excludes.append("nadare.backends.fgo")
# The optional keyring backends are imported dynamically too.
if collect_submodules is not None:
    try:
        hiddenimports += collect_submodules("keyring.backends")
    except Exception:
        pass

# curl_cffi is imported lazily inside the CZN backend functions, so PyInstaller
# cannot see it. It ships a vendored libcurl as the compiled `_wrapper.abi3.so`
# plus `_cffi_backend`; collect the submodules and the shared objects explicitly.
hiddenimports += [
    "curl_cffi",
    "curl_cffi.requests",
    "curl_cffi.const",
    "curl_cffi.requests.exceptions",
]
binaries = []
if collect_dynamic_libs is not None:
    try:
        binaries += collect_dynamic_libs("curl_cffi")
    except Exception:
        pass

a = Analysis(
    [os.path.join(ROOT, "packaging", "nadare_entry.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Keeps the bundle smaller and avoids shipping a Tcl/Tk we never use.
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="nadare",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX can trip packagers/AV and breaks mutability
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="nadare",
)
