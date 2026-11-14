# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""What the prefix needs before the game runs: WebView2, the VC++ runtime and the PCSDK stub.

Split out of :mod:`nadare.backends.czn.backend`; the code is unchanged.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

from ....core import (
    paths,
    vcredist,
    webview2,
)
from ....core.backends import (
    BackendError,
)
from ....core.events import Event
from .shared import (
    PREFIX_MARKER,
    _profile_setting,
)

# Paths below were written relative to backend.py; this code moved to backend_parts/ (#1248),
# so they still resolve from backend.py's own location.
_MODULE_FILE = Path(__file__).resolve().parent.parent / "backend.py"

log = logging.getLogger("nadare.backends.czn.backend")

STUB_NAME = "czn_pcsdk_stub.exe"

#: Both stubs ship prebuilt so users need no mingw-w64; each is used only when it
#: matches its digest. The default is the Go build (``scripts/build_stub_go.sh``,
#: reproducible in a pinned container); the C build (``scripts/build_stub.sh
#: --container``) ships for one release as a fallback. A profile picks the C stub
#: with ``pcsdk_stub = "c"``, or the environment with ``NADARE_CZN_STUB=c``.
STUB_PREBUILT = Path(_MODULE_FILE).resolve().parents[2] / "assets" / STUB_NAME

STUB_PREBUILT_SHA256 = "0a8cc27d2b6a7eb5097d65ca04214de997693da34483e11bc5d76211caddbdf7"

STUB_C_NAME = "czn_pcsdk_stub_c.exe"

STUB_C_PREBUILT = Path(_MODULE_FILE).resolve().parents[2] / "assets" / STUB_C_NAME

STUB_C_PREBUILT_SHA256 = "fe4a5bad970750e974cf999e31e3b87132fbb25f9c04c9eea69e3acc2e243185"

WEBVIEW2_URL = webview2.DEFAULT_URL

#: The bootstrapper is fetched through a mutable Microsoft redirector, so the
#: download is accepted only when it lands on a Microsoft host and matches a
#: pinned SHA-256. AUDIT H1: the hash is never guessed; it is resolved at run
#: time from the ``NADARE_WEBVIEW2_SHA256`` environment variable or the
#: profile's ``webview2_sha256`` setting, with this constant only as the final
#: fallback. When no pin is configured the installer is refused *before* it is
#: downloaded rather than run unverified (never silently bypassed).
#: Pinned 2026-10-13 from the Evergreen standalone x64 installer (Microsoft
#: Last-Modified 2026-10-08, 214090448 B). Microsoft re-issues this file with each
#: Edge release, so the pin goes stale; override with NADARE_WEBVIEW2_SHA256 or
#: the profile's ``webview2_sha256`` until it is bumped.
WEBVIEW2_SHA256: str | None = webview2.PINNED_SHA256

VCREDIST_SHA256: str | None = vcredist.VCREDIST_SHA256


def stub_identity(path: Path) -> tuple[str | None, bool]:
    """``(sha256[:12], is the shipped stub)`` of ``path``; ``(None, False)`` if unreadable."""
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None, False
    return digest[:12], digest in (STUB_PREBUILT_SHA256, STUB_C_PREBUILT_SHA256)


def _stub_label(path: Path) -> str:
    """``(Go)``, ``(C)`` or ``(not shipped)``: which build ``path`` is, for the launch log."""
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "(unreadable)"
    if digest == STUB_PREBUILT_SHA256:
        return "(Go)"
    if digest == STUB_C_PREBUILT_SHA256:
        return "(C)"
    return "(not shipped)"


class PrereqsMixin:
    """Mixin for :class:`CznBackend`; see the module text."""

    def _stub(self) -> Path:
        return self.ctx.profile_dir("stub") / STUB_NAME

    def _shipped_stub(self) -> tuple[Path, str, str]:
        """``(asset, digest, label)`` of the shipped stub this profile should run."""
        raw = os.environ.get("NADARE_CZN_STUB") or _profile_setting(self.ctx.profile, "pcsdk_stub")
        choice = str(raw or "").strip().lower()
        if choice == "c":
            return STUB_C_PREBUILT, STUB_C_PREBUILT_SHA256, "C"
        return STUB_PREBUILT, STUB_PREBUILT_SHA256, "Go"

    def _stub_log(self) -> Path:
        d = paths.logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.ctx.name}-czn-stub.log"

    # ---- install / update --------------------------------------------------
    def _ensure_prefix(self) -> None:
        prefix = self.prefix()
        if (prefix / "drive_c").is_dir():
            return
        # Only a tree this call creates is cleaned up on failure: an existing prefix (even a
        # half-built one the user pointed us at) is never deleted.
        if prefix.exists() and not prefix.is_dir():
            # A file where the prefix goes is a clear error, not a raw FileExistsError (#1207 b011).
            raise BackendError(
                f"{prefix} is a file, not a folder: move it away or choose another place", 1
            )
        created = not prefix.exists()
        try:
            # An existing empty folder is as good as a new one; one holding anything else is
            # the user's, and must never be stamped as Nadare's (uninstall deletes stamped ones).
            ours = created or not any(prefix.iterdir())
        except OSError:
            ours = False
        prefix.mkdir(parents=True, exist_ok=True)
        self.ctx.emit(Event("step", f"Creating the Wine prefix at {prefix}…"))
        try:
            with self._activity_label("Creating the Wine prefix"):
                code = self._run_in_prefix(["wineboot", "-u"])
            if code != 0 or not (prefix / "drive_c").is_dir():
                raise BackendError(
                    self._explain(f"prefix creation failed (exit {code})"),
                    1,
                )
        except BaseException:
            # A failed or cancelled wineboot can leave drive_c behind, which the next run
            # would take for a finished prefix.
            if created:
                shutil.rmtree(prefix, ignore_errors=True)
            raise
        self._write_prefix_markers(prefix, self.runner_name(), created=ours)

    @staticmethod
    def _write_prefix_markers(prefix: Path, runner: str, *, created: bool = True) -> None:
        """Record who made the prefix without writing through a symlink planted at a name.

        ``created=False`` (a folder that already held the user's files) records only the runner.
        """
        markers = [("czn-runner", runner + "\n")]
        if created:
            markers.insert(0, (PREFIX_MARKER, "created by Nadare\n"))
        try:
            for name, text in markers:
                fd, tmp = tempfile.mkstemp(dir=prefix, prefix=f".{name}.")
                try:
                    with os.fdopen(fd, "w") as handle:
                        handle.write(text)
                    os.replace(tmp, prefix / name)
                except OSError:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                    raise
        except OSError:
            pass

    @staticmethod
    def _stub_connected(log: Path) -> bool:
        """True once the stub reports the game opened the launcher pipe."""
        try:
            text = log.read_text(errors="replace")
        except OSError:
            return False
        return "game connected" in text or "heartbeats started" in text

    def _webview2_installed(self) -> bool:
        """Whether the WebView2 runtime is in the prefix and registered, not just on disk."""
        return webview2.installed(self.prefix()) and webview2.registered(self.prefix())

    def _webview2_pin(self) -> str:
        """Resolve the expected installer SHA-256 from a trusted source."""
        return webview2.resolve_pin(self.ctx.profile, WEBVIEW2_SHA256)

    def _webview2_overridden(self) -> bool:
        """True when the user pinned a hash explicitly (env or profile)."""
        return webview2.overridden(self.ctx.profile)

    @staticmethod
    def _webview2_index_pins() -> set[str]:
        """Installer hashes published in the Nadare index (empty on any error)."""
        return webview2.index_pins()

    @staticmethod
    def _webview2_index_source() -> str:
        """The immutable per-build installer URL published in the index."""
        return webview2.index_source()

    def _verify_webview2(self, path: Path) -> None:
        """Refuse a WebView2 installer that does not match the pinned SHA-256."""
        webview2.verify(
            path,
            profile=self.ctx.profile,
            cancel=self.ctx.cancel,
            constant=WEBVIEW2_SHA256,
            index_pins_fn=self._webview2_index_pins,
            overridden_fn=self._webview2_overridden,
        )

    def _download_webview2(self, url: str, tmp: Path, inst: Path) -> None:
        """Download ``url`` to ``tmp``, verify it, and move it to ``inst``."""
        webview2.download(
            url,
            tmp,
            inst,
            verify_fn=self._verify_webview2,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
        )

    def _ensure_webview2(self) -> None:
        webview2.ensure(
            self.prefix(),
            profile=self.ctx.profile,
            cache_dir_fn=self._cache_dir,
            run_in_prefix=self._run_in_prefix,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
            constant=WEBVIEW2_SHA256,
            url=WEBVIEW2_URL,
            installed_fn=self._webview2_installed,
            pin_fn=self._webview2_pin,
            index_pins_fn=self._webview2_index_pins,
            index_source_fn=self._webview2_index_source,
            overridden_fn=self._webview2_overridden,
            verify_fn=self._verify_webview2,
            download_fn=self._download_webview2,
            installer_environment=self._installer_environment,
            activity_label=self._activity_label,
            explain=self._explain,
        )

    # ---- Visual C++ runtime (shared in nadare.core.vcredist) ---------------
    def _vcredist_installed(self) -> bool:
        """Whether the real (not Wine-builtin) VC++ runtime is in the prefix."""
        return vcredist.installed(self.prefix())

    def _vcredist_pin(self) -> str:
        """Resolve the expected installer SHA-256 from a trusted source."""
        return vcredist.resolve_pin(self.ctx.profile, VCREDIST_SHA256)

    def _vcredist_overridden(self) -> bool:
        """True when the user pinned a hash explicitly (env or profile)."""
        return vcredist.overridden(self.ctx.profile)

    @staticmethod
    def _vcredist_index_pins() -> set[str]:
        """Installer hashes published in the Nadare index (empty on any error)."""
        return vcredist.index_pins()

    @staticmethod
    def _vcredist_index_source() -> str:
        """The immutable per-build installer URL published in the index."""
        return vcredist.index_source()

    def _verify_vcredist(self, path: Path) -> None:
        """Refuse an installer that matches no accepted pin (deletes it)."""
        vcredist.verify(
            path,
            profile=self.ctx.profile,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
            constant=VCREDIST_SHA256,
            index_pins_fn=self._vcredist_index_pins,
            overridden_fn=self._vcredist_overridden,
        )

    def _download_vcredist(self, url: str, tmp: Path, inst: Path) -> None:
        """Download ``url`` to ``tmp``, verify it, and move it to ``inst``."""
        vcredist.download(
            url,
            tmp,
            inst,
            verify_fn=self._verify_vcredist,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
        )

    def _install_vcredist(self) -> None:
        """Download, verify and run the VC++ redistributable (may raise)."""
        with self._installer_environment(), self._activity_label(
            "Installing the Visual C++ runtime"
        ):
            self._install_vcredist_inner()

    def _install_vcredist_inner(self) -> None:
        try:
            vcredist.install(
                self.prefix(),
                profile=self.ctx.profile,
                cache_dir=self._cache_dir(),
                run_in_prefix=self._run_in_prefix,
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
                constant=VCREDIST_SHA256,
                index_pins_fn=self._vcredist_index_pins,
                index_source_fn=self._vcredist_index_source,
                overridden_fn=self._vcredist_overridden,
                verify_fn=self._verify_vcredist,
                download_fn=self._download_vcredist,
            )
        except vcredist.StepSkipped:
            raise  # the user chose Skip: ensure carries on without the runtime
        except BackendError as e:
            raise BackendError(self._explain(e.message), e.code) from e

    def _ensure_vcredist(self) -> None:
        """Best-effort VC++ runtime install; never aborts the caller.

        Always runs for CZN (it is not a user setting; an old ``vcredist: false`` in
        a profile is ignored). Only a user cancellation propagates.
        """
        with self._installer_environment():
            vcredist.ensure(
                self.prefix(),
                profile=self.ctx.profile,
                cache_dir=self._cache_dir(),
                run_in_prefix=self._run_in_prefix,
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
                constant=VCREDIST_SHA256,
                index_pins_fn=self._vcredist_index_pins,
                index_source_fn=self._vcredist_index_source,
                overridden_fn=self._vcredist_overridden,
                verify_fn=self._verify_vcredist,
                download_fn=self._download_vcredist,
                install_fn=self._install_vcredist,
            )

    def _ensure_stub(self) -> Path:
        stub = self._stub()
        source, digest, _ = self._shipped_stub()
        if stub.is_file():
            return self._refresh_stub(stub)
        if self._install_prebuilt_stub(stub, source, digest):
            self._note_stub(stub)
            return stub
        script = Path(_MODULE_FILE).with_name("build_stub.sh")
        if not script.is_file():
            raise BackendError("PCSDK stub is missing and build_stub.sh was not found", 1)
        stub.parent.mkdir(parents=True, exist_ok=True)
        env = self._env()
        env["CZN_STUB_OUT"] = str(stub)
        self.ctx.emit(Event("step", "Building the PCSDK stub (mingw-w64)…"))
        code = self._stream(["bash", str(script)], env)
        if code != 0 or not stub.is_file():
            raise BackendError("PCSDK stub build failed (install mingw-w64)", 1)
        self._note_stub(stub)
        return stub

    def _note_stub(self, stub: Path) -> None:
        """Log which stub this profile runs; the log header names the build, this names the stub."""
        sha, shipped = stub_identity(stub)
        if sha is not None:
            kind = "the shipped one" if shipped else "not the shipped one"
            self.info(f"PCSDK stub {_stub_label(stub)}: sha256 {sha}… ({kind})")

    def _refresh_stub(self, stub: Path) -> Path:
        """Keep a profile's stub in step with the selected shipped one, never losing the old copy.

        A copy made by an older Nadare (or swapped in by hand) would otherwise run forever;
        that includes the other shipped build when the profile switched between them.
        """
        source, digest, _ = self._shipped_stub()
        try:
            current = hashlib.sha256(stub.read_bytes()).hexdigest()
        except OSError:
            return stub  # unreadable: let the launch report whatever is wrong with it
        sha = current[:12]
        if current == digest:
            self._note_stub(stub)
            return stub
        backup = stub.with_name(stub.name + ".old")
        try:
            os.replace(stub, backup)
        except OSError:
            self._note_stub(stub)
            return stub
        if self._install_prebuilt_stub(stub, source, digest):
            self.info(
                f"PCSDK stub {_stub_label(stub)}: sha256 {sha}… differed from the shipped one; "
                f"replaced it (the old copy is kept as {backup.name})"
            )
            return stub
        try:
            os.replace(backup, stub)
        except OSError as exc:
            raise BackendError(f"could not restore the PCSDK stub: {exc}", 1) from exc
        self.info(
            f"PCSDK stub: sha256 {sha}… differs from the shipped one; kept "
            "(no verified copy to replace it)"
        )
        return stub

    @staticmethod
    def _install_prebuilt_stub(
        dest: Path, source: Path | None = None, digest: str | None = None
    ) -> bool:
        """Copy the bundled stub to ``dest`` if present and digest-verified."""
        source = STUB_PREBUILT if source is None else source
        digest = STUB_PREBUILT_SHA256 if digest is None else digest
        try:
            data = source.read_bytes()
        except OSError:
            return False
        if hashlib.sha256(data).hexdigest() != digest:
            return False
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(data)
            tmp.chmod(0o755)
            tmp.replace(dest)
        except OSError:
            return False
        return True
