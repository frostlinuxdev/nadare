#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Write the body of a signed ``runners.json`` from a runner archive.

A maintainer points this at the downloaded x86_64 archive and the tested tag; it
records the sha256 and size Nadare will check, then prints the one command that
signs the result with the release key. It never signs or publishes anything: the
signature and the index push stay manual and happen only on the user's go.

The body is what gets signed::

    {"epoch": 9, "runners": {"dwproton": {"tag": ..., "sha256": ..., ...}}}

Run it from the source checkout::

    python scripts/make_runners_json.py dwproton-11.0-19 \\
        /path/to/dwproton-11.0-19-x86_64.tar.xz --epoch 9 \\
        --test-report https://example.org/nadare/test-reports/dwproton-11.0-19

To publish a pin for one family while keeping the others, pass the current body
with ``--previous``. For a signed rollback, add ``--rollback``: the epoch becomes
the previous epoch plus one and the replaced tag is recorded in ``previous_tag``.

See docs/RUNNER-INDEX.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
_SRC = str(REPO / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from nadare.core import releasesig, updatecheck  # noqa: E402
from nadare.core.runnerindex import plain_file_name  # noqa: E402
from nadare.core.runners import RUNNERS  # noqa: E402

TAG_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")


class ManifestError(ValueError):
    """The inputs would make a manifest Nadare refuses."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _previous_body(path: str | None) -> dict:
    if not path:
        return {}
    try:
        doc = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read --previous {path}: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("runners"), dict):
        raise ManifestError("--previous is not a runners.json body")
    if type(doc.get("epoch")) is not int or doc["epoch"] < 1:
        raise ManifestError("--previous has no valid epoch")
    return doc


def _infer_report(args: argparse.Namespace) -> str:
    if args.test_report:
        return args.test_report
    if args.untested:
        return ""
    raise ManifestError("--test-report is required for a tested build")


def _make_row(args: argparse.Namespace) -> dict:
    archive = Path(args.archive).expanduser()
    if not archive.is_file():
        raise ManifestError(f"archive not found: {archive}")
    if not TAG_RE.fullmatch(args.tag):
        raise ManifestError(f"invalid tag {args.tag!r}")
    spec = RUNNERS[args.family]
    name = archive.name
    if spec.tested is None or spec.pinned_asset is None:
        raise ManifestError(f"{spec.name} has no built-in pin to derive an asset name from")
    expected = spec.pinned_asset[0].replace(spec.tested[0], args.tag)
    file_name = None
    if name == expected:
        if not re.fullmatch(spec.asset, name):
            raise ManifestError(f"{name} is not a {spec.name} archive")
    else:
        # An upstream naming change is spelled in the manifest's optional ``file``.
        if args.tag not in name:
            raise ManifestError(
                f"{name} does not name the tag {args.tag!r}; the app would refuse the body"
            )
        try:
            plain_file_name(name)
        except ValueError as exc:
            raise ManifestError(
                f"{name} is not the archive for {args.tag} (the app derives {expected!r} "
                "from its built-in pin, and file must be a plain .tar.xz/.tar.gz/.tar.zst name)"
            ) from exc
        file_name = name
    try:
        date.fromisoformat(args.tested_date)
    except ValueError as exc:
        raise ManifestError(f"invalid --tested-date {args.tested_date!r}") from exc
    if args.published_at:
        try:
            published = datetime.fromisoformat(args.published_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ManifestError(f"invalid --published-at {args.published_at!r}") from exc
        if published.tzinfo is None:
            raise ManifestError("--published-at needs a timezone (for example a trailing Z)")
    row: dict = {
        "tag": args.tag,
        "sha256": sha256_file(archive),
        "size": archive.stat().st_size,
        "published_at": args.published_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "tested": {"yes": not args.untested, "date": args.tested_date},
    }
    if file_name is not None:
        row["file"] = file_name
    report = _infer_report(args)
    if report:
        parsed = urlparse(report)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ManifestError("--test-report must be an https link")
        row["tested"]["report"] = report
    if args.critical:
        row["critical"] = True
    if args.min_nadare:
        if updatecheck.version_key(args.min_nadare) is None:
            raise ManifestError(f"invalid --min-nadare {args.min_nadare!r}")
        row["min_nadare"] = args.min_nadare
    if args.previous_tag:
        if not TAG_RE.fullmatch(args.previous_tag):
            raise ManifestError(f"invalid --previous-tag {args.previous_tag!r}")
        row["previous_tag"] = args.previous_tag
    return row


def build_body(args: argparse.Namespace) -> dict:
    if args.family not in RUNNERS:
        raise ManifestError(f"unknown runner family {args.family!r}")
    previous = _previous_body(args.previous)
    prior_row = (previous.get("runners") or {}).get(args.family)
    if args.rollback and not previous:
        raise ManifestError("--rollback needs --previous so the replaced tag is known")
    row = _make_row(args)
    if args.rollback and not row.get("previous_tag"):
        prior_tag = prior_row.get("tag") if isinstance(prior_row, dict) else None
        if not isinstance(prior_tag, str):
            raise ManifestError("--rollback found no previous tag for this family")
        row["previous_tag"] = prior_tag
    if args.epoch is None:
        if not previous:
            raise ManifestError("--epoch is required without --previous")
        epoch = previous["epoch"] + 1
    else:
        epoch = args.epoch
        if epoch < 1:
            raise ManifestError("--epoch must be at least 1")
        if previous and epoch <= previous["epoch"]:
            raise ManifestError(
                f"--epoch {epoch} must be higher than the previous epoch {previous['epoch']}"
            )
    runners = dict(previous.get("runners") or {})
    runners[args.family] = row
    return {"epoch": epoch, "runners": runners}


def write_body(path: Path, body: dict) -> None:
    """Replace ``path`` atomically so a failed run leaves the old file alone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(body, indent=2, sort_keys=True) + "\n"
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def parser() -> argparse.ArgumentParser:
    today = date.today().isoformat()
    p = argparse.ArgumentParser(
        description="Write a runners.json body; signing and publishing stay manual.",
    )
    p.add_argument("tag", help="tested runner tag, as it appears in the archive name")
    p.add_argument("archive", help="the downloaded x86_64 archive to hash")
    p.add_argument("--family", default="dwproton", choices=sorted(RUNNERS))
    p.add_argument("--epoch", type=int, default=None,
                   help="manifest epoch; must increase for every publication")
    p.add_argument("--previous", default="",
                   help="current runners.json body: keep its other families and epoch")
    p.add_argument("--rollback", action="store_true",
                   help="signal a signed rollback; needs --previous and records its tag")
    p.add_argument("--published-at", default="",
                   help="upstream release time (ISO 8601 with timezone; default: now)")
    p.add_argument("--tested-date", default=today, help=f"test date (default: {today})")
    p.add_argument("--test-report", default="", help="https link to the test report")
    p.add_argument("--untested", action="store_true",
                   help="mark the build as not tested (Nadare keeps the built-in pin)")
    p.add_argument("--critical", action="store_true",
                   help="offer this pin to games still on the previous recommended build")
    p.add_argument("--min-nadare", default="", help="oldest Nadare version that may use this pin")
    p.add_argument("--previous-tag", default="",
                   help="for a signed rollback: the tag this one replaces")
    p.add_argument("-o", "--output", required=True, help="file to write (the JSON body)")
    return p


def _numbers(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", tag))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        body = build_body(args)
    except (ManifestError, OSError) as exc:
        print(f"make_runners_json: {exc}", file=sys.stderr)
        return 2
    output = Path(args.output).expanduser()
    write_body(output, body)
    row = body["runners"][args.family]
    floor = RUNNERS[args.family].tested
    if floor and _numbers(row["tag"]) < _numbers(floor[0]):
        print(
            f"warning: {row['tag']} is older than Nadare's built-in pin {floor[0]}; "
            "every Nadare released so far refuses this body and keeps its own pin",
            file=sys.stderr,
        )
    print(f"wrote {output} ({row['size']} bytes, sha256 {row['sha256']})")
    print("This script signs and publishes nothing. Sign the body by hand with the release key")
    print(f"({releasesig.RELEASE_KEY.fingerprint}):")
    print(
        "  gpg --armor --detach-sign "
        f"--local-user {releasesig.RELEASE_KEY.fingerprint} "
        f"--output {shlex.quote(str(output) + '.asc')} {shlex.quote(str(output))}"
    )
    print("Then copy both files to the index root and publish it on the user's go.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
