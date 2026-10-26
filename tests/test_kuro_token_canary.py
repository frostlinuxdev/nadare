# SPDX-License-Identifier: GPL-3.0-or-later
"""The Kuro token canary warns when a published launcher token is no longer accepted."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_kuro_tokens.py"
PUBLISH_SCRIPT = REPO / "scripts" / "publish_index.py"

TOKENS = {"version": 1, "tokens": {"wuwa": "50004_abcd1234", "pgr": "50015_efgh5678"}}


def _canary():
    spec = importlib.util.spec_from_file_location("kuro_token_canary", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_tokens(root: Path, value=TOKENS) -> Path:
    path = root / "catalog" / "kuro-tokens.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    return root


def fake_fetch(status: int = 200, body: str = '{"default": {}}'):
    calls: list[str] = []

    def fetch(url: str) -> tuple[int, str]:
        calls.append(url)
        return status, body

    return fetch, calls


def test_valid_tokens_hit_the_kuro_launcher_index() -> None:
    canary = _canary()
    fetch, calls = fake_fetch()
    assert canary.check(TOKENS["tokens"], fetch) == []
    assert calls == [
        "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G153/50004_abcd1234/index.json",
        "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G143/50015_efgh5678/index.json",
    ]


@pytest.mark.parametrize(
    "value", ["60004_abcd1234", "50004_bad-token!", "not-a-token", "50004_short"]
)
def test_a_malformed_published_token_is_a_problem(value: str) -> None:
    canary = _canary()
    fetch, calls = fake_fetch()
    problems = canary.check({"wuwa": value}, fetch)
    assert len(problems) == 1
    assert "wuwa" in problems[0] and "malformed" in problems[0]
    assert calls == []  # nothing to ask Kuro about


@pytest.mark.parametrize("code", [403, 404])
def test_a_refused_token_is_a_problem(code: int) -> None:
    canary = _canary()
    fetch, _calls = fake_fetch(code, "")
    problems = canary.check({"wuwa": TOKENS["tokens"]["wuwa"]}, fetch)
    assert len(problems) == 1
    assert "wuwa" in problems[0]
    assert f"HTTP {code}" in problems[0]
    assert "kuro-tokens.json" in problems[0]


def test_a_server_error_is_only_a_note(capsys: pytest.CaptureFixture[str]) -> None:
    canary = _canary()
    fetch, _calls = fake_fetch(503, "")
    assert canary.check({"wuwa": TOKENS["tokens"]["wuwa"]}, fetch) == []
    assert "kuro tokens" in capsys.readouterr().err


def test_a_network_error_is_only_a_note(capsys: pytest.CaptureFixture[str]) -> None:
    canary = _canary()

    def fetch(url: str) -> tuple[int, str]:
        raise TimeoutError("offline")

    assert canary.check({"pgr": TOKENS["tokens"]["pgr"]}, fetch) == []
    assert "kuro tokens" in capsys.readouterr().err


@pytest.mark.parametrize("body", ["<html>error</html>", "{}", "[]", "null", "not json"])
def test_an_unexpected_page_is_a_problem(body: str) -> None:
    canary = _canary()
    fetch, _calls = fake_fetch(200, body)
    problems = canary.check({"wuwa": TOKENS["tokens"]["wuwa"]}, fetch)
    assert len(problems) == 1
    assert "wuwa" in problems[0] and "unexpected page" in problems[0]


def test_an_empty_published_table_checks_nothing() -> None:
    canary = _canary()
    fetch, calls = fake_fetch()
    assert canary.check({}, fetch) == []
    assert calls == []


@pytest.mark.parametrize("content", [None, "not json", "[]"])
def test_main_without_readable_tokens_checks_the_builtin_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], content
) -> None:
    canary = _canary()
    if content is not None:
        path = tmp_path / "catalog" / "kuro-tokens.json"
        path.parent.mkdir()
        path.write_text(content, encoding="utf-8")
    fetch, calls = fake_fetch()
    monkeypatch.setattr(canary, "_fetch", fetch)
    assert canary.main(["--index", str(tmp_path)]) == 0
    assert "checking the built-in tokens" in capsys.readouterr().out
    assert len(calls) == 2
    assert any("/G153/50004_" in url for url in calls)
    assert any("/G143/50015_" in url for url in calls)


def test_a_refused_builtin_token_is_reported_when_nothing_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = _canary()
    fetch, _calls = fake_fetch(403, "")
    monkeypatch.setattr(canary, "_fetch", fetch)
    assert canary.main(["--index", str(tmp_path)]) == 4
    assert capsys.readouterr().out.count("CANARY: Kuro token for") == 2


def test_main_exits_4_and_prints_canary_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = _canary()
    index = write_tokens(tmp_path)

    def fetch(url: str) -> tuple[int, str]:
        return 403, ""

    monkeypatch.setattr(canary, "_fetch", fetch)
    assert canary.main(["--index", str(index)]) == 4
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("CANARY:")]
    assert len(lines) == 2
    assert any("wuwa" in line for line in lines)
    assert any("pgr" in line for line in lines)


def _publish_module():
    spec = importlib.util.spec_from_file_location("publish_index_under_test", PUBLISH_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_publishing_the_catalog_never_touches_the_kuro_tokens(tmp_path: Path) -> None:
    pi = _publish_module()
    out = tmp_path / "catalog"
    out.mkdir()
    tokens = out / "kuro-tokens.json"
    tokens.write_text(json.dumps(TOKENS) + "\n", encoding="utf-8")
    before = tokens.read_bytes()
    # An unreadable index makes publish_catalog rebuild it from that run.
    (out / "index.json").write_text("{broken", encoding="utf-8")
    game = pi.CatalogGame("Alpha Game", lambda: "1.2", None)
    pi.publish_catalog(out, {}, games={"alpha": game})
    pi.publish_catalog(out, {}, games={"alpha": game})
    assert tokens.exists()
    assert tokens.read_bytes() == before


def test_a_gzip_compressed_index_body_is_decoded() -> None:
    import gzip

    canary = _canary()
    body = '{"default": {"cdnList": []}}'
    assert canary.decode_body(gzip.compress(body.encode())) == body
    assert canary._has_default(canary.decode_body(gzip.compress(body.encode())))
    assert canary.decode_body(body.encode()) == body
    assert canary.decode_body(b"\x1f\x8bnot really gzip") != ""  # never raises
