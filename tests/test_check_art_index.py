# SPDX-License-Identifier: GPL-3.0-or-later
"""The art-index canary: a game whose index art all fails turns the heartbeat down."""

from __future__ import annotations

import http.server
import importlib.util
import json
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_art_index.py"
URL_A = "https://cdn2.unrealengine.com/a.jpg"
URL_B = "https://cdn2.unrealengine.com/b.jpg"


def _canary():
    spec = importlib.util.spec_from_file_location("art_index_canary", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.BLIND.clear()
    return module


def _doc(*urls, kind="direct-genshin"):
    covers = [{"url": u, "orientation": "wide", "width": 2560, "height": 1440} for u in urls]
    return {"schema": 1, "games": {kind: {"cover": covers}}}


def _head(table):
    calls = []

    def head(url):
        calls.append(url)
        return table.get(url, (404, "text/html", 0))

    return head, calls


OK = (200, "image/jpeg", 400_000)


def test_a_game_whose_first_candidate_works_is_fine():
    canary = _canary()
    head, calls = _head({URL_A: OK})
    assert canary.check(_doc(URL_A, URL_B), head) == []
    assert calls == [URL_A], "later candidates are not probed when the first one works"


def test_a_dead_first_candidate_with_a_working_second_is_only_a_note(capsys):
    canary = _canary()
    head, _ = _head({URL_B: OK})
    assert canary.check(_doc(URL_A, URL_B), head) == []
    assert "direct-genshin" in capsys.readouterr().err


def test_a_game_with_no_working_candidate_is_a_problem():
    canary = _canary()
    head, _ = _head({})
    problems = canary.check(_doc(URL_A, URL_B), head)
    assert len(problems) == 1 and "direct-genshin" in problems[0]


@pytest.mark.parametrize(
    "answer", [(200, "text/html", 5000), (200, "image/jpeg", 100), (301, "", 0), (500, "", 0)]
)
def test_an_answer_that_is_not_a_real_image_does_not_count(answer):
    canary = _canary()
    head, _ = _head({URL_A: answer})
    assert len(canary.check(_doc(URL_A), head)) == 1


def test_an_unknown_size_is_accepted_when_the_type_is_an_image():
    canary = _canary()
    head, _ = _head({URL_A: (200, "image/webp", None)})
    assert canary.check(_doc(URL_A), head) == []


def test_a_candidate_the_app_would_reject_is_reported_without_a_request():
    canary = _canary()
    head, calls = _head({"https://evil.example/a.jpg": OK})
    doc = _doc("https://evil.example/a.jpg")
    problems = canary.check(doc, head)
    assert problems and "not acceptable" in problems[0]
    assert calls == []


def test_a_network_error_is_blind_not_a_problem():
    canary = _canary()

    def head(url):
        raise TimeoutError("offline")

    assert canary.check(_doc(URL_A), head) == []
    assert canary.BLIND


def test_main_exits_4_and_prints_canary_lines(tmp_path, monkeypatch, capsys):
    canary = _canary()
    path = tmp_path / "catalog" / "art.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_doc(URL_A)))
    head, _ = _head({})
    monkeypatch.setattr(canary, "_head", head)
    assert canary.main(["--index", str(tmp_path)]) == 4
    assert "CANARY:" in capsys.readouterr().out


def test_main_is_fine_when_the_index_has_no_art_document(tmp_path, capsys):
    canary = _canary()
    assert canary.main(["--index", str(tmp_path)]) == 0
    assert "no art document" in capsys.readouterr().out


def test_main_reports_a_broken_document(tmp_path, capsys):
    canary = _canary()
    path = tmp_path / "catalog" / "art.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    assert canary.main(["--index", str(tmp_path)]) == 4
    assert "CANARY:" in capsys.readouterr().out


def test_the_seed_document_has_the_shape_the_canary_reads():
    canary = _canary()
    seed = json.loads((REPO / "docs" / "art-index.seed.json").read_text("utf-8"))
    head, calls = _head({})
    problems = canary.check(seed, head)
    assert len(problems) == len(seed["games"]), "every game is probed (and fails with no network)"


# ---- review fixes: redirects, shapes, blind handling ------------------------------------------


@pytest.fixture
def server():
    """A local HTTP server: /ok, /redirect, /nohead (405 on HEAD), /empty, /small."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, headers, body=b""):
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self._route()

        def do_GET(self):
            self._route()

        def _route(self):
            body = b"x" * 50_000
            if self.path == "/ok":
                self._send(
                    200, {"Content-Type": "image/jpeg", "Content-Length": str(len(body))}, body
                )
            elif self.path == "/redirect":
                self._send(302, {"Location": "/ok", "Content-Length": "0"})
            elif self.path == "/nohead":
                if self.command == "HEAD":
                    self._send(405, {"Content-Length": "0"})
                else:
                    self._send(
                        206,
                        {
                            "Content-Type": "image/jpeg",
                            "Content-Range": "bytes 0-0/50000",
                            "Content-Length": "1",
                        },
                        b"x",
                    )
            elif self.path == "/empty":
                self._send(200, {"Content-Type": "image/jpeg", "Content-Length": "0"})
            else:
                self._send(404, {"Content-Length": "0"})

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_head_does_not_follow_a_redirect(server):
    canary = _canary()
    assert canary._head(server + "/redirect")[0] == 302
    assert not canary._works(canary._head(server + "/redirect"))


def test_a_server_that_refuses_head_is_probed_with_a_ranged_get(server):
    canary = _canary()
    answer = canary._head(server + "/nohead")
    assert canary._works(answer)
    assert answer[2] == 50_000, "the real size comes from Content-Range"


def test_an_explicit_zero_length_is_not_a_working_image(server):
    canary = _canary()
    assert not canary._works(canary._head(server + "/empty"))


def test_a_plain_ok_answer_works(server):
    canary = _canary()
    assert canary._works(canary._head(server + "/ok"))


@pytest.mark.parametrize("games", [[], "x", 5, None])
def test_a_games_value_of_the_wrong_type_is_a_problem_not_a_crash(games):
    canary = _canary()
    head, _ = _head({})
    problems = canary.check({"schema": 1, "games": games}, head)
    assert problems


@pytest.mark.parametrize("doc", [{}, [], {"schema": 1}, {"schema": 1, "games": {}}, "x", None])
def test_a_document_with_nothing_usable_is_not_ok(doc):
    canary = _canary()
    head, _ = _head({})
    assert canary.check(doc, head)


def test_a_network_error_on_the_first_candidate_still_tries_the_next_one():
    canary = _canary()
    calls = []

    def head(url):
        calls.append(url)
        if url == URL_A:
            raise TimeoutError("flaky")
        return OK

    assert canary.check(_doc(URL_A, URL_B), head) == []
    assert calls == [URL_A, URL_B]
    assert not canary.BLIND, "a working later candidate settles the game"


def test_a_game_that_could_only_be_reached_with_errors_is_blind_and_main_reports_it(
    tmp_path, monkeypatch, capsys
):
    canary = _canary()
    path = tmp_path / "catalog" / "art.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_doc(URL_A)))

    def boom(url):
        raise TimeoutError("offline")

    monkeypatch.setattr(canary, "_head", boom)
    assert canary.main(["--index", str(tmp_path)]) == 4  # "could not check" is not "fine"
    assert "blind" in capsys.readouterr().out
