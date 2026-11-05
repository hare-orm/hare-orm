from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from tests.matrix.run_spec import RunSpec


@dataclass(frozen=True, slots=True)
class RunResult:
    """Observed process outcome, kept separate from the requested matrix cell."""

    spec: RunSpec
    exit_code: int
    counts: dict[str, int]
    duration_seconds: float
    log_path: Path
    collected: int | None = None
    seed: int | str | None = None
    tool_versions: dict[str, str] = field(default_factory=dict)
    report_error: str | None = None

    @property
    def passed(self) -> bool:
        return self.report_error is None and (
            self.exit_code == 0 or (self.exit_code == 5 and self.spec.allow_empty and self.collected == 0)
        )

    @classmethod
    def read(cls, spec: RunSpec, exit_code: int, duration: float, log_path: Path) -> RunResult:
        """Read the controller's report; missing data never turns a process into a success."""
        report = {}
        error = None
        try:
            report = json.loads(log_path.with_suffix(".json").read_text(encoding="utf-8"))
            if report["exit_code"] != exit_code:
                error = f"pytest report status {report['exit_code']} differs from process status {exit_code}"
            report["counts"] = dict(report["counts"])
            report["tool_versions"] = dict(report["tool_versions"])
            report["collected"]
            report["seed"]
        except (OSError, ValueError, KeyError, TypeError) as report_error:
            error = f"Missing or invalid pytest report: {report_error}"
            report = {}
        return cls(
            spec,
            exit_code,
            report.get("counts", {}),
            duration,
            log_path,
            collected=report.get("collected"),
            seed=report.get("seed"),
            tool_versions=report.get("tool_versions", {}),
            report_error=error,
        )
