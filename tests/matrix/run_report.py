from __future__ import annotations

import importlib.metadata
import json
import os
import platform
from pathlib import Path

import pytest

from tests.matrix.constants import RUN_REPORT_ENVIRONMENT_VARIABLE, RUN_REPORT_PACKAGES


class RunReport:
    """Write pytest's structured outcomes and the actual seed/tool versions of this process."""

    @staticmethod
    def write(session: pytest.Session) -> None:
        target = os.environ.get(RUN_REPORT_ENVIRONMENT_VARIABLE)
        # xdist workers inherit the destination; only the controller owns the aggregate report.
        if not target or hasattr(session.config, "workerinput"):
            return
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        versions = {"python": platform.python_version()}
        for package in RUN_REPORT_PACKAGES:
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                continue
        report = {
            "exit_code": int(session.exitstatus),
            "collected": session.testscollected,
            "counts": {
                ("errors" if name == "error" else name): len(reports)
                for name, reports in reporter.stats.items()
                if name in {"passed", "failed", "error", "skipped", "xfailed", "xpassed", "deselected"}
            },
            "seed": session.config.getoption("randomly_seed", default=None),
            "tool_versions": versions,
        }
        Path(target).write_text(json.dumps(report, indent=2), encoding="utf-8")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session: pytest.Session):
    result = yield
    RunReport.write(session)
    return result
