# SPDX-License-Identifier: GPL-3.0-or-later
"""The single adapter for legacy backend integrity reports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

VerifyOutcome = Literal["passed", "failed", "nothing_to_verify", "not_installed"]


@dataclass(frozen=True)
class VerifyResult:
    outcome: VerifyOutcome
    message: str = ""
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome not in ("passed", "failed", "nothing_to_verify", "not_installed"):
            raise ValueError(f"unknown verify outcome: {self.outcome!r}")

    @classmethod
    def from_report(cls, report: object) -> VerifyResult:
        if isinstance(report, cls):
            return report
        if not isinstance(report, Mapping):
            return cls("failed", "File check failed")
        # All shipped verify implementations supply a boolean `verified`. Absence
        # is a malformed report, never evidence of a successful check.
        outcome = report.get("outcome")
        if outcome is None and type(report.get("verified")) is not bool:
            return cls("failed", "File check failed", dict(report))
        if outcome is None:
            outcome = "passed" if report.get("verified") is True else "failed"
        if outcome not in ("passed", "failed", "nothing_to_verify", "not_installed"):
            return cls("failed", "File check failed", dict(report))
        if "verified" in report and (
            type(report["verified"]) is not bool or report["verified"] != (outcome == "passed")
        ):
            return cls("failed", "File check failed", dict(report))
        message = report.get("message", "")
        return cls(
            cast(VerifyOutcome, outcome), message if isinstance(message, str) else "", dict(report)
        )

    def as_report(self) -> dict[str, object]:
        return {
            **self.details,
            "outcome": self.outcome,
            "verified": self.outcome == "passed",
            "message": self.message,
        }
