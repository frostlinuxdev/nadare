"""PRIVACY.md names every host Nadare can contact, and says what really goes there (#810)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVACY = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")

#: Strings that look like hosts but are never contacted: XML namespaces in SVG/HTML parsing
#: and licence texts shipped with fonts and 7-Zip.
NOT_CONTACTED = {"w3.org", "sil.org", "gnu.org"}
#: The project's own host is configured in core/hosting.py and named generically in the doc
#: ("the project host"); it differs between the internal and the public build.
SKIP_FILES = {"core/hosting.py"}

_URL = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _domain(host: str) -> str:
    return ".".join(host.lower().split(".")[-2:])


def _project_domains() -> set[str]:
    """The project host(s) from core/hosting.py, whatever this build points them at."""
    from urllib.parse import urlparse

    from nadare.core import hosting

    out = set()
    for value in vars(hosting).values():
        if isinstance(value, str) and value.startswith("https://"):
            host = urlparse(value).hostname or ""
            if host and _domain(host) != "github.com":
                out.add(_domain(host))
    return out


def _source_domains() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    src = ROOT / "src" / "nadare"
    for path in src.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".json", ".txt", ".svg", ".html"}:
            continue
        rel = path.relative_to(src).as_posix()
        if rel in SKIP_FILES:
            continue
        for host in _URL.findall(path.read_text(encoding="utf-8", errors="replace")):
            if host.startswith(("example.", "localhost")) or host.endswith(".invalid"):
                continue
            found.setdefault(_domain(host), set()).add(rel)
    return found


def test_every_contacted_domain_is_named_in_privacy_md():
    missing = {
        domain: sorted(files)[:3]
        for domain, files in _source_domains().items()
        if domain not in NOT_CONTACTED
        and domain not in _project_domains()
        and domain not in PRIVACY
    }
    assert not missing, f"add these hosts to PRIVACY.md: {missing}"


def test_the_six_wrong_statements_are_gone():
    text = " ".join(PRIVACY.split())
    assert "sends nothing about you to anyone" not in text
    assert "The one automatic request" not in text
    assert "| GitHub Releases | CZN runner download |" not in PRIVACY
    assert "nothing about you and no identifier" not in text
    assert "when a game page or the library is shown" not in text


def test_what_is_said_instead():
    text = " ".join(PRIVACY.split())
    assert "IP address" in text and "User-Agent" in text
    assert "device key" in text  # publisher sign-ins (STOVE, Netmarble)
    assert "notices" in text and "update check" in text
    assert "not affected by" in text  # news and art are not behind the update-check setting
