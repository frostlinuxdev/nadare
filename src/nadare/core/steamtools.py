# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Pure core of the Steam client helpers (``docs/STEAM-H264.md`` and the
Compatibility-tool half of ``docs/STEAM-PREFIX-FIX.md``).

Three separate concerns live here, all testable without a real Steam:

* whether the client is running, and the close/start command lists (nothing
  is executed here);
* the H.264 command and the reported game list;
* a minimal, byte-preserving editor for the ``CompatToolMapping`` section of
  ``config.vdf``, plus a dated backup before any write.

Nadare never installs system codecs, and never reformats ``config.vdf``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import shlex
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import paths
from .backends import BackendError

#: The client command when the profile does not name one.
DEFAULT_STEAM_BINARY = "steam"


# ---- Steam running -----------------------------------------------------


def _argv(entry: Path) -> list[str] | None:
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _is_client(argv: list[str]) -> bool:
    """The native client, its helpers, or a Flatpak/Snap invocation of it."""
    if not argv:
        return False
    if any("com.valvesoftware.Steam" in token for token in argv):
        return True
    if any(token == "steam" or token.endswith("/steam") for token in argv[:2]):
        return True
    return Path(argv[0]).name in {"steam", "steam.sh", "steamwebhelper"}


def steam_pids(*, proc_root: str | os.PathLike[str] = "/proc") -> list[int]:
    """PIDs of this user's Steam client (or helpers); empty when it is closed."""
    me = os.getpid()
    uid = os.getuid()
    found: list[int] = []
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        argv = _argv(entry)
        if argv and _is_client(argv):
            found.append(int(entry.name))
    return sorted(found)


def steam_running(*, proc_root: str | os.PathLike[str] = "/proc") -> bool:
    """Whether the Steam client is running (edit ``config.vdf`` only when it is not)."""
    return bool(steam_pids(proc_root=proc_root))


# ---- command lists -----------------------------------------------------


def steam_command(binary: str | None = None) -> list[str]:
    """Split the configured client command (a Flatpak client carries arguments)."""
    configured = str(binary or DEFAULT_STEAM_BINARY).strip() or DEFAULT_STEAM_BINARY
    try:
        parts = shlex.split(configured)
    except ValueError as exc:
        raise BackendError(f"invalid Steam command {configured!r}: {exc}") from exc
    return parts or [DEFAULT_STEAM_BINARY]


def shutdown_command(binary: str | None = None) -> list[str]:
    """``steam -shutdown`` (the client closes cleanly and flushes ``config.vdf``)."""
    return [*steam_command(binary), "-shutdown"]


def start_command(binary: str | None = None) -> list[str]:
    return steam_command(binary)


def restart_plan(binary: str | None = None) -> list[list[str]]:
    """The close-then-start command lists; the caller runs them, not this module."""
    return [shutdown_command(binary), start_command(binary)]


def unlockh264_command(binary: str | None = None) -> list[str]:
    """The client's own H.264 enable request (a single global action)."""
    return [*steam_command(binary), "steam://unlockh264/"]


# ---- H.264 game list ---------------------------------------------------


@dataclass(frozen=True)
class H264Game:
    game: str
    symptom: str
    recommended: tuple[str, ...]
    status: str  # "reported" until someone tests it
    #: Filled in only after the id was looked up and confirmed.
    app_id: str = ""


#: Data, not code; seed entries come from user reports (see the design doc).
H264_GAMES: tuple[H264Game, ...] = (
    H264Game("Uma Musume", "white screen", ("dwproton", "ge"), "reported", "3224770"),
    H264Game("Snowbreak", "video / startup", ("dwproton", "ge"), "reported", "2668080"),
)


def h264_game(query: str | int) -> H264Game | None:
    """Look up a reported game by name (case-insensitive) or confirmed app id."""
    key = str(query).strip()
    if not key:
        return None
    folded = key.casefold()
    return next(
        (
            game
            for game in H264_GAMES
            if game.game.casefold() == folded or (game.app_id and game.app_id == key)
        ),
        None,
    )


# ---- a minimal, byte-preserving VDF reader/editor ---------------------


@dataclass(frozen=True)
class _Tok:
    kind: str  # "str" | "bare" | "open" | "close"
    value: str
    start: int
    end: int


@dataclass
class _Block:
    key: str
    key_tok: _Tok
    open_tok: _Tok
    close_tok: _Tok | None
    items: list[object]  # _Block | (key_tok, value_tok)


_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def _tokenize(text: str) -> list[_Tok]:
    """Quoted strings (with escapes), bare tokens, braces; ``//`` comments skipped."""
    tokens: list[_Tok] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char in " \t\r\n\f\v":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif char == "{":
            tokens.append(_Tok("open", "{", i, i + 1))
            i += 1
        elif char == "}":
            tokens.append(_Tok("close", "}", i, i + 1))
            i += 1
        elif char == '"':
            start = i
            i += 1
            value: list[str] = []
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    value.append(_ESCAPES.get(text[i + 1], text[i + 1]))
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                value.append(text[i])
                i += 1
            tokens.append(_Tok("str", "".join(value), start, i))
        else:
            start = i
            while i < n and text[i] not in ' \t\r\n\f\v{}"':
                i += 1
            tokens.append(_Tok("bare", text[start:i], start, i))
    return tokens


def _parse(tokens: list[_Tok], index: int = 0) -> tuple[list[object], _Tok | None, int]:
    """Parse one block's children until its ``}``; returns (items, close, next index)."""
    items: list[object] = []
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "close":
            return items, token, index + 1
        if token.kind == "open":  # malformed leading brace: skip it
            index += 1
            continue
        key = token
        index += 1
        if index >= len(tokens):
            break
        following = tokens[index]
        if following.kind == "open":
            children, close, index = _parse(tokens, index + 1)
            items.append(_Block(key.value, key, following, close, children))
        elif following.kind == "close":
            items.append((key, None))
        else:
            items.append((key, following))
            index += 1
    return items, None, index


def _blocks(items: list[object]) -> list[_Block]:
    return [item for item in items if isinstance(item, _Block)]


def _find(items: list[object], path: tuple[str, ...]) -> _Block | None:
    current = items
    node: _Block | None = None
    for key in path:
        node = next((block for block in _blocks(current) if block.key == key), None)
        if node is None:
            return None
        current = node.items
    return node


def _search(items: list[object], key: str) -> _Block | None:
    """Depth-first search for the first block with ``key`` (fallback layout)."""
    for block in _blocks(items):
        if block.key == key:
            return block
        found = _search(block.items, key)
        if found is not None:
            return found
    return None


_STEAM_PATH = ("InstallConfigStore", "Software", "Valve", "Steam")
_MAPPING_PATH = (*_STEAM_PATH, "CompatToolMapping")


def _mapping_node(items: list[object]) -> _Block | None:
    return _find(items, _MAPPING_PATH) or _search(items, "CompatToolMapping")


def _steam_node(items: list[object]) -> _Block | None:
    return _find(items, _STEAM_PATH) or _search(items, "Steam")


def _line_start(text: str, position: int) -> int:
    return text.rfind("\n", 0, position) + 1


def _indent_at(text: str, position: int) -> str:
    start = _line_start(text, position)
    prefix = text[start:position]
    return prefix if not prefix.strip() else ""


def _indent_unit(text: str) -> str:
    """The file's first indentation step: a tab, or its first run of spaces."""
    for line in text.splitlines():
        if line.startswith("\t"):
            return "\t"
        stripped = line.lstrip(" ")
        if stripped and len(stripped) < len(line):
            return " " * (len(line) - len(stripped))
    return "    "


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fields(block: _Block) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in block.items:
        if isinstance(item, tuple) and item[1] is not None:
            key, value = item
            out[key.value] = value.value
    return out


def _block_span(text: str, block: _Block) -> tuple[int, int]:
    """The block's bytes including its line indentation, excluding the newline."""
    start = _line_start(text, block.key_tok.start)
    if text[start:block.key_tok.start].strip():
        start = block.key_tok.start
    end = block.close_tok.end if block.close_tok is not None else block.key_tok.end
    return start, end


def _entry_blocks(mapping: _Block, app_id: str) -> list[_Block]:
    return [block for block in _blocks(mapping.items) if block.key == app_id]


def _single_entry(mapping: _Block, app_id: str) -> _Block | None:
    entries = _entry_blocks(mapping, app_id)
    if len(entries) > 1:
        raise BackendError(
            f"config.vdf has {len(entries)} duplicate CompatToolMapping entries for app "
            f"{app_id}; refusing to guess which one Steam uses"
        )
    return entries[0] if entries else None


@dataclass(frozen=True)
class CompatEntry:
    """One game's ``CompatToolMapping`` entry as it is in the file."""

    app_id: str
    fields: Mapping[str, str]
    #: The exact block text (with indentation), used to restore it byte for byte.
    text: str = field(repr=False)

    @property
    def tool(self) -> str:
        return str(self.fields.get("name", ""))


def read_compat_entry(text: str, app_id: str | int) -> CompatEntry | None:
    """The current mapping entry for ``app_id``, or ``None`` when it has none."""
    key = str(app_id).strip()
    if not key:
        return None
    items, _close, _index = _parse(_tokenize(text))
    mapping = _mapping_node(items)
    if mapping is None:
        return None
    entry = _single_entry(mapping, key)
    if entry is None:
        return None
    start, end = _block_span(text, entry)
    return CompatEntry(app_id=key, fields=_fields(entry), text=text[start:end])


#: Steam's 64-bit ids of individual accounts start here; the folder number of an account under
#: ``userdata/`` is its id minus this base.
_STEAM_ID_BASE = 76561197960265728


def most_recent_user(loginusers_text: str) -> str | None:
    """The 64-bit Steam id of the account that signed in last (``loginusers.vdf``), or ``None``."""
    if not loginusers_text:
        return None
    items, _close, _index = _parse(_tokenize(loginusers_text))
    users = _find(items, ("users",))
    if users is None:
        return None
    for block in _blocks(users.items):
        if _fields(block).get("MostRecent") == "1":
            return block.key
    return None


def account_id(steam_id: str) -> int | None:
    """The ``userdata/<number>`` folder number of a 64-bit Steam id; ``None`` when it is not one."""
    text = str(steam_id).strip()
    if not (text.isascii() and text.isdigit()):
        return None
    value = int(text)
    return value - _STEAM_ID_BASE if value > _STEAM_ID_BASE else None


def read_flag(text: str, key: str) -> str | None:
    """The value of the first ``"key" "value"`` pair named ``key`` anywhere in a VDF text.

    Read only: used for single settings such as ``UnlockedH264Feature`` in an account's
    ``localconfig.vdf``. ``None`` when the key is absent or the text is empty.
    """
    if not text or not key:
        return None
    items, _close, _index = _parse(_tokenize(text))

    def walk(nodes: list[object]) -> str | None:
        for node in nodes:
            if isinstance(node, _Block):
                found = walk(node.items)
                if found is not None:
                    return found
            elif isinstance(node, tuple) and node[0].value == key and node[1] is not None:
                return str(node[1].value)
        return None

    return walk(items)


_LOCAL_APPS_PATH = ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps")
_LAUNCH_KEY = "LaunchOptions"


def _launch_fields(block: _Block) -> list[tuple[_Tok, _Tok]]:
    return [
        item for item in block.items
        if isinstance(item, tuple) and item[1] is not None and item[0].value == _LAUNCH_KEY
    ]


def _local_app_block(text: str, app_id: str) -> tuple[_Block, _Block | None]:
    """The ``apps`` section of a ``localconfig.vdf`` and this game's block in it (if any)."""
    items, _close, _index = _parse(_tokenize(text))
    apps = _find(items, _LOCAL_APPS_PATH)
    if apps is None:
        raise BackendError(
            "localconfig.vdf has no Software/Valve/Steam/apps section; start Steam once first"
        )
    if apps.close_tok is None:
        raise BackendError("localconfig.vdf's apps section is not closed; refusing to edit")
    found = [block for block in _blocks(apps.items) if block.key == app_id]
    if len(found) > 1:
        raise BackendError(f"localconfig.vdf has more than one block for app {app_id}")
    return apps, (found[0] if found else None)


def read_launch_options(text: str, app_id: str | int) -> str | None:
    """The launch options this account set for a game, or ``None`` when it has none."""
    key = str(app_id).strip()
    if not text or not key:
        return None
    try:
        _apps, block = _local_app_block(text, key)
    except BackendError:
        return None
    if block is None:
        return None
    fields = _launch_fields(block)
    return str(fields[-1][1].value) if fields else None


def edit_launch_options_text(text: str, app_id: str | int, value: str | None) -> str:
    """Set (or with ``None`` remove) one game's launch options; every other byte stays."""
    key = str(app_id).strip()
    apps, block = _local_app_block(text, key)
    eol = _eol(text)
    unit = _indent_unit(text)
    if block is None:
        if value is None:
            return text
        existing = _blocks(apps.items)
        app_indent = (
            _indent_at(text, existing[0].key_tok.start)
            if existing
            else _indent_at(text, apps.key_tok.start) + unit
        )
        child = app_indent + unit
        at = _line_start(text, apps.close_tok.start)
        lead = ""
        if text[at : apps.close_tok.start].strip():
            at, lead = apps.close_tok.start, eol
        new = (
            f'{lead}{app_indent}"{key}"{eol}{app_indent}{{{eol}'
            f'{child}"{_LAUNCH_KEY}"\t\t{_quote(value)}{eol}{app_indent}}}{eol}'
        )
        return text[:at] + new + text[at:]
    if block.close_tok is None:
        raise BackendError(f"localconfig.vdf's block for app {key} is not closed; refusing to edit")
    fields = _launch_fields(block)
    if len(fields) > 1:
        raise BackendError(f"app {key} has more than one {_LAUNCH_KEY} entry; fix it by hand first")
    if fields:
        key_tok, value_tok = fields[0]
        if value is not None:
            return text[: value_tok.start] + _quote(value) + text[value_tok.end :]
        start = _line_start(text, key_tok.start)
        if text[start : key_tok.start].strip():
            start = key_tok.start
        end = value_tok.end
        while end < len(text) and text[end] in " \t":
            end += 1
        if text.startswith("\r\n", end):
            end += 2
        elif end < len(text) and text[end] == "\n":
            end += 1
        return text[:start] + text[end:]
    if value is None:
        return text
    children = _blocks(block.items) or [item for item in block.items if isinstance(item, tuple)]
    first = children[0]
    anchor = first.key_tok if isinstance(first, _Block) else first[0]
    child_indent = _indent_at(text, anchor.start) or (_indent_at(text, block.key_tok.start) + unit)
    insert_at = block.open_tok.end
    if text.startswith("\r\n", insert_at):
        insert_at += 2
        lead = ""
    elif insert_at < len(text) and text[insert_at] == "\n":
        insert_at += 1
        lead = ""
    else:
        lead = eol
    line = f'{child_indent}"{_LAUNCH_KEY}"\t\t{_quote(value)}{eol}'
    return text[:insert_at] + lead + line + text[insert_at:]


@dataclass(frozen=True)
class LaunchEdit:
    """What :func:`apply_launch_options` did to one game's launch options."""

    path: Path
    app_id: str
    value: str | None
    changed: bool
    #: The options before the change (``None``: the game had none). Pass it back to undo.
    previous: str | None
    backup: Path | None


def apply_launch_options(
    path: Path,
    app_id: str | int,
    value: str | None | Callable[[str | None], str | None],
    *,
    steam_running_fn=steam_running,
    today: str | None = None,
    lock_dir: Path | None = None,
) -> LaunchEdit:
    """Set a game's launch options in an account's ``localconfig.vdf``, after a dated backup.

    Refuses while Steam runs (it rewrites the file on exit). An unchanged value writes nothing.
    """
    path = Path(path)
    with config_lock(path, lock_dir=lock_dir):
        original = _check_editable(path, steam_running_fn)
        previous = read_launch_options(original, app_id)
        # A function decides from what is there now, read inside the lock.
        wanted = value(previous) if callable(value) else value
        updated = edit_launch_options_text(original, app_id, wanted)
        if updated == original:
            return LaunchEdit(path, str(app_id), wanted, False, previous, None)
        backup = _dated_backup(path, today)
        _write_atomic(path, updated)
    return LaunchEdit(path, str(app_id), wanted, True, previous, backup)


def _insert_entry(text: str, mapping: _Block, app_id: str, tool: str) -> str:
    """Add an app entry as the last child of ``mapping`` (indentation preserved)."""
    eol = _eol(text)
    unit = _indent_unit(text)
    existing = _blocks(mapping.items)
    if existing:
        app_indent = _indent_at(text, existing[0].key_tok.start) or unit
    else:
        mapping_indent = _indent_at(text, mapping.key_tok.start)
        if mapping.key_tok.start == _line_start(text, mapping.key_tok.start):
            mapping_indent = ""
        app_indent = mapping_indent + unit
    child_indent = app_indent + unit
    if mapping.close_tok is None:
        raise BackendError("config.vdf's CompatToolMapping section is not closed; refusing to edit")
    at = _line_start(text, mapping.close_tok.start)
    if text[at:mapping.close_tok.start].strip():
        at = mapping.close_tok.start
        eol_lead = eol
    else:
        eol_lead = ""
    block = (
        f"{app_indent}{_quote(app_id)}{eol}"
        f"{app_indent}{{{eol}"
        f'{child_indent}"name"\t\t{_quote(tool)}{eol}'
        f'{child_indent}"config"\t\t""{eol}'
        f'{child_indent}"Priority"\t\t"250"{eol}'
        f"{app_indent}}}{eol}"
    )
    return text[:at] + eol_lead + block + text[at:]


def _insert_mapping_section(text: str, items: list[object], app_id: str, tool: str) -> str:
    steam = _steam_node(items)
    if steam is None:
        raise BackendError(
            "config.vdf has no Steam section, so there is nowhere to put CompatToolMapping"
        )
    if steam.close_tok is None:
        raise BackendError("config.vdf's Steam section is not closed; refusing to edit")
    eol = _eol(text)
    unit = _indent_unit(text)
    children = _blocks(steam.items)
    if children:
        mapping_indent = _indent_at(text, children[0].key_tok.start) or unit
    else:
        steam_indent = _indent_at(text, steam.key_tok.start)
        mapping_indent = steam_indent + unit
    at = _line_start(text, steam.close_tok.start)
    if text[at:steam.close_tok.start].strip():
        at = steam.close_tok.start
        lead = eol
    else:
        lead = ""
    section = (
        f"{mapping_indent}\"CompatToolMapping\"{eol}"
        f"{mapping_indent}{{{eol}"
        f"{mapping_indent}}}{eol}"
    )
    with_section = text[:at] + lead + section + text[at:]
    # The new (empty) section is now the one the normal editor fills.
    return edit_compat_text(with_section, app_id, tool)


def edit_compat_text(text: str, app_id: str | int, tool: str) -> str:
    """Set the ``name`` of one app's mapping entry; change nothing else.

    Only that app's ``name`` value is replaced, or a ``name`` field (or a whole
    entry, or the whole ``CompatToolMapping`` section) is inserted when it is
    missing.  Indentation, tabs, spacing, comments, line endings and every
    other entry are left exactly as they are.
    """
    key = str(app_id).strip()
    if not key.isdigit():
        raise BackendError(f"Steam app id must be numeric, got {key!r}")
    items, _close, _index = _parse(_tokenize(text))
    mapping = _mapping_node(items)
    if mapping is None:
        return _insert_mapping_section(text, items, key, tool)
    entry = _single_entry(mapping, key)
    if entry is None:
        return _insert_entry(text, mapping, key, tool)
    if entry.close_tok is None:
        # A file cut off in the middle (a crash while Steam wrote it): the entry's text is
        # unknown, so neither the edit nor its undo could be trusted.
        raise BackendError(f"the CompatToolMapping entry for app {key} is not closed")

    names = [
        item for item in entry.items
        if isinstance(item, tuple) and item[1] is not None and item[0].value == "name"
    ]
    if len(names) > 1:
        raise BackendError(
            f"the CompatToolMapping entry for app {key} has more than one name field; "
            "fix it by hand first"
        )
    for item in entry.items:
        if not isinstance(item, tuple) or item[1] is None:
            continue
        key_tok, value_tok = item
        if key_tok.value != "name":
            continue
        if value_tok.value == tool:
            return text
        return text[: value_tok.start] + _quote(tool) + text[value_tok.end :]

    # No name field: add one as the first child of this entry only.
    if entry.close_tok is None:
        raise BackendError(f"the CompatToolMapping entry for app {key} is not closed")
    eol = _eol(text)
    children = _blocks(entry.items)
    if children:
        child_indent = _indent_at(text, children[0].key_tok.start)
    else:
        child_indent = _indent_at(text, entry.key_tok.start) + _indent_unit(text)
    insert_at = entry.open_tok.end
    if insert_at <= len(text) and text[insert_at : insert_at + 2] == "\r\n":
        insert_at += 2
        lead = ""
    elif insert_at < len(text) and text[insert_at] == "\n":
        insert_at += 1
        lead = ""
    else:
        lead = eol
    field_text = f"{child_indent}\"name\"\t\t{_quote(tool)}{eol}"
    return text[:insert_at] + lead + field_text + text[insert_at:]


def restore_compat_text(text: str, app_id: str | int, previous: CompatEntry | None) -> str:
    """Put ``previous`` back exactly, or remove the entry when it was absent."""
    key = str(app_id).strip()
    items, _close, _index = _parse(_tokenize(text))
    mapping = _mapping_node(items)
    entry = _single_entry(mapping, key) if mapping is not None else None
    eol = _eol(text)
    if entry is not None and entry.close_tok is None:
        raise BackendError(f"the CompatToolMapping entry for app {key} is not closed")
    if previous is None:
        if entry is None:
            return text
        start, end = _block_span(text, entry)
        while end < len(text) and text[end] in " \t":
            end += 1
        if text.startswith("\r\n", end):
            end += 2
        elif end < len(text) and text[end] == "\n":
            end += 1
        return text[:start] + text[end:]
    if entry is not None:
        # Keep any newline that follows the old block; replace the block itself.
        start, end = _block_span(text, entry)
        return text[:start] + previous.text + text[end:]
    if mapping is None or mapping.close_tok is None:
        raise BackendError("config.vdf has no CompatToolMapping section to restore into")
    at = _line_start(text, mapping.close_tok.start)
    lead = "" if not text[at:mapping.close_tok.start].strip() else eol
    return text[:at] + lead + previous.text + eol + text[at:]


# ---- config.vdf file operations ---------------------------------------


@dataclass(frozen=True)
class CompatEdit:
    path: Path
    app_id: str
    tool: str
    changed: bool
    previous: CompatEntry | None
    backup: Path | None


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", "surrogateescape")
    except OSError as exc:
        raise BackendError(f"cannot read {path} (config.vdf): {exc}") from exc


def _write_atomic(path: Path, text: str) -> None:
    # A config that is a symlink (dotfiles) keeps being one: the file it points at is replaced.
    path = Path(os.path.realpath(path))
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8", "surrogateescape"))
            handle.flush()
            os.fsync(handle.fileno())  # the data must be on disk before the name points at it
        os.chmod(name, mode)
        os.replace(name, path)
        _sync_folder(path.parent)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _sync_folder(folder: Path) -> None:
    """Flush a folder's entries (best effort: some filesystems cannot open a folder)."""
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _dated_backup(path: Path, today: str | None) -> Path:
    """Copy ``path`` to a dated name nobody else has: the name is claimed atomically."""
    stamp = today or date.today().isoformat()
    suffix = 1
    while True:
        tail = f"-{suffix}" if suffix > 1 else ""
        candidate = path.with_name(f"{path.name}.bak-{stamp}{tail}")
        suffix += 1
        try:
            with open(path, "rb") as source, open(candidate, "xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            shutil.copystat(path, candidate)
            return candidate
        except FileExistsError:  # taken (also a dangling link): try the next number
            continue
        except OSError as exc:
            raise BackendError(f"could not back up {path} to {candidate}: {exc}") from exc


@contextlib.contextmanager
def config_lock(path: Path, *, lock_dir: Path | None = None):
    """Hold a lock that every Nadare process editing this ``config.vdf`` takes in turn.

    Steam itself ignores it; it only keeps two Nadare windows or processes from rebuilding the
    file from stale reads. If the lock file cannot be made the edit goes ahead unlocked.
    """
    folder = Path(lock_dir) if lock_dir is not None else paths.state_dir()
    name = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:16]
    fd = -1
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / f"steam-config-{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        if fd >= 0:
            os.close(fd)
        fd = -1
    try:
        yield
    finally:
        if fd >= 0:
            os.close(fd)  # closing drops the flock


def _check_editable(path: Path, steam_running_fn) -> str:
    if steam_running_fn():
        raise BackendError(
            f"Steam is running, and it rewrites {path.name} when it exits. Close Steam "
            "first (Nadare can do it with steam -shutdown) and try again."
        )
    if not path.is_file():
        raise BackendError(f"{path} ({path.name}) does not exist; start Steam once first")
    return _read(path)


def apply_compat_tool(
    path: Path,
    app_id: str | int,
    tool: str,
    *,
    steam_running_fn=steam_running,
    today: str | None = None,
    lock_dir: Path | None = None,
) -> CompatEdit:
    """Set one game's compatibility tool, after a dated backup of ``config.vdf``.

    Refuses while Steam runs.  Refuses to overwrite a backup (a second edit on
    the same day gets ``-2``).  Reports whether anything changed; an unchanged
    tool writes neither the file nor a backup.
    """
    path = Path(path)
    with config_lock(path, lock_dir=lock_dir):
        original = _check_editable(path, steam_running_fn)  # read inside the lock
        previous = read_compat_entry(original, app_id)
        updated = edit_compat_text(original, app_id, tool)
        if updated == original:
            return CompatEdit(path, str(app_id), tool, False, previous, None)
        backup = _dated_backup(path, today)
        _write_atomic(path, updated)
    return CompatEdit(path, str(app_id), tool, True, previous, backup)


def restore_compat_tool(
    path: Path,
    app_id: str | int,
    previous: CompatEntry | None,
    *,
    steam_running_fn=steam_running,
    today: str | None = None,
    lock_dir: Path | None = None,
) -> CompatEdit:
    """Undo :func:`apply_compat_tool` with the recorded previous entry."""
    path = Path(path)
    tool = previous.tool if previous is not None else ""
    with config_lock(path, lock_dir=lock_dir):
        original = _check_editable(path, steam_running_fn)
        updated = restore_compat_text(original, app_id, previous)
        if updated == original:
            return CompatEdit(path, str(app_id), tool, False, previous, None)
        backup = _dated_backup(path, today)
        _write_atomic(path, updated)
    return CompatEdit(path, str(app_id), tool, True, previous, backup)


__all__ = [
    "DEFAULT_STEAM_BINARY",
    "H264_GAMES",
    "CompatEdit",
    "LaunchEdit",
    "apply_launch_options",
    "config_lock",
    "edit_launch_options_text",
    "read_launch_options",
    "CompatEntry",
    "H264Game",
    "apply_compat_tool",
    "edit_compat_text",
    "h264_game",
    "read_compat_entry",
    "restart_plan",
    "restore_compat_text",
    "restore_compat_tool",
    "shutdown_command",
    "start_command",
    "steam_command",
    "steam_pids",
    "steam_running",
    "unlockh264_command",
]
