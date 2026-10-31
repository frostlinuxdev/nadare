# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the b20 state/config audit items (#6, #85, #242, #255, #622)."""

from __future__ import annotations

import errno
import io
import json
import logging
import os
from pathlib import Path

import pytest

from nadare.core import download as dl
from nadare.core import events
from nadare.core import logging as logging_mod
from nadare.core import secrets as core_secrets
from nadare.core.config import TOP_DEFAULTS, Config
from nadare.core.journal import JournalError, Transaction
from nadare.core.secrets import SecretStore


# ---------------------------------------------------------------------------
# #622: quarantine the bad file, not a symlink that points at it
# ---------------------------------------------------------------------------
def test_quarantine_moves_the_bad_file_not_its_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{not json", encoding="utf-8")
    link = tmp_path / "config.json"
    link.symlink_to(real)

    cfg = Config.load(link)

    assert cfg.notice
    # The unreadable file itself was moved aside, not the link.
    assert not real.exists()
    assert list(tmp_path.glob("real.json.corrupt-*"))
    assert link.is_symlink()
    # A later save recreates the target, so the symlinked name keeps working.
    cfg.save()
    assert json.loads(real.read_text(encoding="utf-8"))["schema"] == 1


# ---------------------------------------------------------------------------
# #622: a save must not resurrect a top-level key another process deleted
# ---------------------------------------------------------------------------
def test_save_does_not_resurrect_a_key_deleted_by_another_process(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    first = Config.load(path)
    first.data["language"] = "de"
    first.save()

    second = Config.load(path)
    del second.data["language"]
    second.save()
    assert "language" not in json.loads(path.read_text(encoding="utf-8"))

    # The first process never touched ``language`` after loading; its unrelated
    # save must leave the deletion alone.
    first.data["check_updates"] = False
    first.save()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["check_updates"] is False
    assert on_disk.get("language", "en") == "en"  # back to the default, not "de"
    # The key stays readable in the saving process: Settings does ``config.data["language"]``.
    assert first.data["language"] == "en"


def test_a_custom_key_deleted_by_another_process_is_not_written_back(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    first = Config.load(path)
    first.data["from_a_newer_nadare"] = {"x": 1}
    first.save()
    second = Config.load(path)
    del second.data["from_a_newer_nadare"]
    second.save()
    first.data["check_updates"] = False
    first.save()
    assert "from_a_newer_nadare" not in json.loads(path.read_text(encoding="utf-8"))
    assert "from_a_newer_nadare" not in first.data


def test_saving_over_an_older_file_keeps_every_default_key_readable(tmp_path: Path) -> None:
    """A file written before a setting existed must not make it vanish from a running process."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"profiles": {}, "active_profile": ""}), encoding="utf-8")
    config = Config.load(path)
    config.data["telemetry"] = False
    config.save()
    config.data["dismissed_update"] = "x"
    config.save()
    for key in TOP_DEFAULTS:
        assert key in config.data, key
    assert config.data["language"] == "en"


# ---------------------------------------------------------------------------
# #622: a filesystem without flock support must not make save() fatal
# ---------------------------------------------------------------------------
def test_save_works_when_flock_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    def no_lock(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(fcntl, "flock", no_lock)
    cfg = Config.load(tmp_path / "config.json")
    cfg.data["language"] = "de"
    cfg.save()

    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["language"] == "de"


# ---------------------------------------------------------------------------
# #85: journal containment must also cover a symlinked intermediate directory
# ---------------------------------------------------------------------------
def test_journal_refuses_a_path_behind_a_symlinked_directory(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "file"
    victim.write_bytes(b"untouched")
    (root / "sub").symlink_to(outside, target_is_directory=True)

    journal = root / ".nadare-update-transaction"
    journal.mkdir()
    (journal / "journal.json").write_text(
        json.dumps(
            {
                "phase": "applying",
                "operations": [
                    {
                        "path": "sub/file",
                        "backup": None,
                        "backup_sha256": None,
                        "directory": False,
                        "temporary": ".nadare-new-1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(JournalError):
        Transaction.load(root)
    with pytest.raises(JournalError):
        Transaction.recover(root)
    assert victim.read_bytes() == b"untouched"


# ---------------------------------------------------------------------------
# #242: quoted multi-word secrets must be redacted whole
# ---------------------------------------------------------------------------
def test_mask_redacts_a_quoted_multi_word_secret() -> None:
    masked = events.mask('password="two word secret"')
    assert "two" not in masked and "secret" not in masked
    assert masked.startswith("password=")

    masked_known = events.mask_known("authorization: 'a b c'")
    assert "b c" not in masked_known


# ---------------------------------------------------------------------------
# #242: tracebacks written to the log must pass through the masker too
# ---------------------------------------------------------------------------
def test_logging_filter_masks_a_secret_in_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nadare.core import logging as nadare_logging

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = logging.getLogger(nadare_logging.LOGGER_NAME)
    saved_handlers = list(root.handlers)
    saved_configured = nadare_logging._configured
    root.handlers.clear()
    nadare_logging._configured = False
    try:
        log_file = tmp_path / "nadare.log"
        logger = nadare_logging.setup(filename=log_file, level=logging.INFO)
        secret = "Q" * 20 + "SUPERSECRET" * 2
        try:
            raise RuntimeError(f"login failed with token={secret}")
        except RuntimeError:
            logger.warning("request failed", exc_info=True)
        for handler in logger.handlers:
            handler.flush()
            handler.close()
        assert secret not in log_file.read_text(encoding="utf-8")
    finally:
        nadare_logging.reset()
        root.handlers[:] = saved_handlers
        nadare_logging._configured = saved_configured


# ---------------------------------------------------------------------------
# #255: malformed Content-Range on the plain and pooled download paths
# ---------------------------------------------------------------------------
class _FakePlainResponse:
    def __init__(self, body: bytes, status: int, headers: dict[str, str]) -> None:
        self._body = body
        self.status = status
        self.headers = headers

    def read(self, n: int = -1) -> bytes:
        data = self._body if n is None or n < 0 else self._body[:n]
        self._body = b"" if n is None or n < 0 else self._body[n:]
        return data

    def __enter__(self) -> _FakePlainResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_download_rejects_a_malformed_content_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"x" * 100
    half = len(body) // 2
    dest = tmp_path / "game.bin"
    (tmp_path / "game.bin.part").write_bytes(body[:half])

    def _open(url: str, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        return _FakePlainResponse(
            body[half:],
            206,
            {"Content-Length": str(len(body) - half), "Content-Range": "bytes nonsense"},
        )

    monkeypatch.setattr(dl, "http_open", _open)
    with pytest.raises(dl.DownloadError, match="malformed Content-Range"):
        dl.download("http://example.invalid/game", dest, retries=1, backoff=0)


class _FakeStreamResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def read(self, n: int = -1) -> bytes:
        data = self._body if n is None or n < 0 else self._body[:n]
        self._body = b"" if n is None or n < 0 else self._body[n:]
        return data

    def close(self) -> None:
        pass


class _FakePool:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    def open(self, url: str, **kwargs: object) -> dl.HttpResponse:
        return dl.HttpResponse(self._response)


def test_stream_download_rejects_a_malformed_content_range(tmp_path: Path) -> None:
    body = b"x" * 100
    half = len(body) // 2
    dest = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(body[:half])
    response = _FakeStreamResponse(
        206,
        {"Content-Length": str(len(body) - half), "Content-Range": "bytes nonsense"},
        body[half:],
    )

    with pytest.raises(dl.DownloadError, match="malformed Content-Range"):
        dl.stream_download(
            "https://x/y", dest, pool=_FakePool(response), expected_size=len(body)
        )


# ---------------------------------------------------------------------------
# #255: the fallback file's chmod-failure branch must stay non-fatal
# ---------------------------------------------------------------------------
def test_fallback_read_survives_a_chmod_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path)
    store.set("game", "token")
    os.chmod(path, 0o644)

    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(core_secrets.os, "chmod", denied)
    assert store.get("game") == "token"


def test_failed_fallback_replace_cleans_up_its_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path)

    def denied(src: object, dst: object) -> None:
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(core_secrets.os, "replace", denied)
    with pytest.raises(PermissionError):
        store.set("game", "token")

    assert not list(tmp_path.glob(".secrets-*.tmp"))


# ---------------------------------------------------------------------------
# #593: the dead module-level plaintext store must stay gone
# ---------------------------------------------------------------------------
def test_secrets_exposes_no_module_level_plaintext_store() -> None:
    assert not hasattr(core_secrets, "_default")
    assert not hasattr(core_secrets, "get_token")
    assert not hasattr(core_secrets, "set_token")
    assert not hasattr(core_secrets, "delete_token")
    assert not hasattr(core_secrets, "keyring_available")


# ---------------------------------------------------------------------------
# #334 F-09: a permission failure deleting the data folder is a warning
# ---------------------------------------------------------------------------
def test_remove_game_warns_when_the_data_folder_cannot_be_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nadare.core import paths, removal, shortcuts

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("keep", backend="wine", title="Keep")
    cfg.add_profile("gone", backend="wine", title="Gone")
    cfg.save()

    apps = tmp_path / "apps"
    shortcuts.install_desktop_entry("gone", "Gone", applications_dir=apps, argv=["nadare"])
    data = paths.profiles_dir() / "gone"
    data.mkdir(parents=True)
    (data / "save.dat").write_text("x", encoding="utf-8")

    def denied(root: Path, keep=()) -> object:
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(removal, "_rmtree_safely", denied)
    result = removal.remove_game(cfg, "gone", delete_data=True, applications_dir=apps)

    # Shortcuts were already removed before the data-folder attempt.
    assert [p.name for p in result.shortcuts_removed] == ["nadare-gone.desktop"]
    assert not (apps / "nadare-gone.desktop").exists()
    assert any("could not remove data dir" in w for w in result.warnings)
    assert data.exists()
    assert result.profile_kept is False
    assert "gone" not in cfg.profiles


# ---------------------------------------------------------------------------
# review wave 3: the three regressions found in the first fix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        'password="ab\\"cd"',
        'password="a"b"',
        "password='it\\'s'",
        'token="two word secret"',
        "password=plain",
    ],
)
def test_a_quoted_secret_is_redacted_to_its_end_even_with_quotes_inside(text):
    masked = events.mask(text)
    assert masked.startswith(text.split("=")[0] + "=<redacted>")
    for leaked in ("cd", "b\"", "s'", "word secret"):
        assert leaked not in masked.split("<redacted>", 1)[1]


def test_a_logged_traceback_keeps_the_exception_type_readable(tmp_path):
    handler_stream = io.StringIO()
    handler = logging.StreamHandler(handler_stream)
    handler.addFilter(logging_mod.MaskingFilter())
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("nadare.test.traceback.keep")
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    try:
        try:
            raise FileNotFoundError(
                "[Errno 2] No such file: /x/y  token=abcdefghijklmnopqrstuvwxyz123456"
            )
        except FileNotFoundError:
            logger.error("install failed", exc_info=True)
    finally:
        logger.removeHandler(handler)
    text = handler_stream.getvalue()
    assert "FileNotFoundError: [Errno 2]" in text  # greppable
    assert "abcdefghijklmnopqrstuvwxyz123456" not in text  # the secret is still gone
    assert "test_a_logged_traceback_keeps_the_exception_type_readable" in text  # frames intact


def test_a_chained_traceback_keeps_every_exception_type(tmp_path):
    handler_stream = io.StringIO()
    handler = logging.StreamHandler(handler_stream)
    handler.addFilter(logging_mod.MaskingFilter())
    logger = logging.getLogger("nadare.test.traceback.chain")
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    secret = "hunter2" * 3  # built at run time: a literal would show in the traceback's source line
    try:
        try:
            try:
                raise PermissionError(f"denied password={secret}")
            except PermissionError as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError:
            logger.error("x", exc_info=True)
    finally:
        logger.removeHandler(handler)
    text = handler_stream.getvalue()
    assert "PermissionError: denied" in text and "RuntimeError: wrapped" in text
    assert "hunter2hunter2hunter2" not in text


def test_ungzip_rejects_a_truncated_stream():
    import gzip

    from nadare.core import directdl
    from nadare.core.sources import ungzip

    data = gzip.compress(b'{"a": 1, "b": 2}')
    with pytest.raises(directdl.DirectError, match="truncated"):
        ungzip(data[:-6])


def test_ungzip_reads_every_member_of_a_multi_member_file():
    import gzip

    from nadare.core.sources import ungzip

    assert ungzip(gzip.compress(b"aaa") + gzip.compress(b"bbb")) == b"aaabbb"


def test_ungzip_limit_counts_all_members():
    import gzip

    from nadare.core import directdl
    from nadare.core.sources import ungzip

    two = gzip.compress(b"a" * 600) + gzip.compress(b"b" * 600)
    with pytest.raises(directdl.DirectError, match="exceeds"):
        ungzip(two, limit=1000)
