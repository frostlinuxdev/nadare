# SPDX-License-Identifier: GPL-3.0-or-later
"""core.paths.check_relative: one engine, three named policies for untrusted manifest paths."""

from __future__ import annotations

import pytest

from nadare.core import directdl, paths, sophon, yostar
from nadare.core.sophon import SophonError
from nadare.core.yostar import ManifestError


@pytest.mark.parametrize(
    ("policy", "value", "expected"),
    [
        (paths.DIRECT_PATHS, "a/b/c.txt", "a/b/c.txt"),
        (paths.DIRECT_PATHS, "./a//b/", "a/b"),
        (paths.SOPHON_PATHS, "a/b/c.txt", "a/b/c.txt"),
        (paths.MANIFEST_PATHS, "a\\b", "a/b"),
        (paths.MANIFEST_PATHS, "a/../b", "b"),
        (paths.MANIFEST_PATHS, "  a/./b  ", "a/b"),
    ],
)
def test_accepted_paths_are_normalised_per_policy(policy, value, expected):
    assert paths.check_relative(value, policy) == expected


@pytest.mark.parametrize(
    ("policy", "value", "code"),
    [
        (paths.DIRECT_PATHS, 5, "not-string"),
        (paths.DIRECT_PATHS, "", "empty"),
        (paths.DIRECT_PATHS, "a\\b", "backslash"),
        (paths.DIRECT_PATHS, "/a", "absolute"),
        (paths.DIRECT_PATHS, "a/../b", "dotdot"),
        (paths.DIRECT_PATHS, "./", "empty-normalised"),
        (paths.SOPHON_PATHS, "a//b", "component"),
        (paths.SOPHON_PATHS, "a/", "component"),
        (paths.SOPHON_PATHS, "a/..", "component"),
        (paths.SOPHON_PATHS, "a\\b", "backslash"),
        (paths.MANIFEST_PATHS, "   ", "empty"),
        (paths.MANIFEST_PATHS, "a\x00b", "nul"),
        (paths.MANIFEST_PATHS, "C:/a", "absolute"),
        (paths.MANIFEST_PATHS, "\\a", "absolute"),
        (paths.MANIFEST_PATHS, "../a", "escape"),
        (paths.MANIFEST_PATHS, "a/../../b", "escape"),
        (paths.MANIFEST_PATHS, ".", "escape"),
    ],
)
def test_rejected_paths_carry_a_reason_code(policy, value, code):
    with pytest.raises(paths.PathProblem) as caught:
        paths.check_relative(value, policy)
    assert caught.value.code == code


def test_the_policies_differ_where_the_old_validators_differed():
    assert paths.check_relative("a/./b", paths.DIRECT_PATHS) == "a/b"
    with pytest.raises(paths.PathProblem):
        paths.check_relative("a/./b", paths.SOPHON_PATHS)
    with pytest.raises(paths.PathProblem):
        paths.check_relative("a\\b", paths.DIRECT_PATHS)
    assert paths.check_relative("a\\b", paths.MANIFEST_PATHS) == "a/b"


@pytest.mark.parametrize(
    "func",
    [directdl.safe_relative_path, sophon._safe_relative_path, yostar.safe_relative_path],
    ids=["direct", "sophon", "yostar"],
)
def test_every_validator_goes_through_the_engine(func, monkeypatch):
    calls = []
    real = paths.check_relative

    def spy(path, policy):
        calls.append(policy)
        return real(path, policy)

    monkeypatch.setattr(paths, "check_relative", spy)
    assert func("a/b") == "a/b"
    assert len(calls) == 1


def test_each_module_uses_its_own_policy(monkeypatch):
    seen = {}
    real = paths.check_relative

    def spy(path, policy):
        seen[path] = policy
        return real(path, policy)

    monkeypatch.setattr(paths, "check_relative", spy)
    directdl.safe_relative_path("d/x")
    sophon._safe_relative_path("s/x")
    yostar.safe_relative_path("y/x")
    assert seen == {
        "d/x": paths.DIRECT_PATHS,
        "s/x": paths.SOPHON_PATHS,
        "y/x": paths.MANIFEST_PATHS,
    }


class _BoomRepr:
    def __repr__(self):
        raise RuntimeError("repr exploded")


class _CountingStr(str):
    calls = 0

    def __repr__(self):
        type(self).calls += 1
        return super().__repr__()


@pytest.mark.parametrize(
    ("func", "error"),
    [
        (directdl.safe_relative_path, directdl.DirectError),
        (sophon._safe_relative_path, SophonError),
        (yostar.safe_relative_path, RuntimeError),
    ],
    ids=["direct", "sophon", "yostar"],
)
def test_a_hostile_repr_does_not_change_the_exception_type(func, error):
    with pytest.raises(error):
        func(_BoomRepr())


@pytest.mark.parametrize(
    ("func", "bad"),
    [
        (directdl.safe_relative_path, "a/../b"),
        (sophon._safe_relative_path, "a//b"),
        (yostar.safe_relative_path, "../b"),
    ],
    ids=["direct", "sophon", "yostar"],
)
def test_a_rejected_path_is_formatted_once_and_an_accepted_one_never(func, bad):
    _CountingStr.calls = 0
    func(_CountingStr("a/b"))
    assert _CountingStr.calls == 0
    with pytest.raises(Exception):  # noqa: B017, PT011 - each validator has its own type
        func(_CountingStr(bad))
    assert _CountingStr.calls == 1


@pytest.mark.parametrize(
    ("func", "error"),
    [
        (directdl.safe_relative_path, directdl.DirectError),
        (sophon._safe_relative_path, SophonError),
        (yostar.safe_relative_path, ManifestError),
    ],
    ids=["direct", "sophon", "yostar"],
)
def test_an_unknown_engine_code_still_raises_the_validators_own_error(func, error, monkeypatch):
    def new_rule(path, policy):
        raise paths.PathProblem("a-rule-nobody-wrote-a-message-for")

    monkeypatch.setattr(paths, "check_relative", new_rule)
    with pytest.raises(error):
        func("a/b")
