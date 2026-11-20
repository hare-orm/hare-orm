from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from tests.matrix.constants import EVERY_SUITE_ENVIRONMENT
from tests.matrix.matrix_catalog import MatrixCatalog

if TYPE_CHECKING:
    from tests.matrix.run_spec import RunSpec


class ContractCoverage:
    """Report selected pytest cases and require evidence in their declared matrix suites.

    Selectors are node IDs, optionally ending at the test name to include its parameter cases.
    Requirements apply to the all-extras environment; an extras-only run may collect just a
    subset of the modules. The declared suites determine applicability independently of pytest.
    """

    @staticmethod
    def get_selectors() -> dict[str, list[str]]:
        return MatrixCatalog.get("contracts")

    @staticmethod
    def get_outcomes(stats: Mapping[str, list[Any]]) -> dict[str, dict[str, str]]:
        """Collect selected outcomes, with failed setup or teardown taking precedence."""
        selectors = ContractCoverage.get_selectors()
        outcomes: dict[str, dict[str, str]] = {}
        for status in ("deselected", "passed", "skipped", "xfailed", "xpassed", "failed", "error"):
            for report in stats.get(status, ()):
                node_id = report.nodeid
                if node_id not in selectors and node_id.partition("[")[0] not in selectors:
                    continue
                outcome = {"status": status}
                if status in {"skipped", "xfailed"}:
                    reason = getattr(report, "longrepr", None)
                    outcome["reason"] = str(reason[2] if isinstance(reason, tuple) else reason or status)
                outcomes[node_id] = outcome
        return outcomes

    @staticmethod
    def get_missing(run: RunSpec, outcomes: Mapping[str, Mapping[str, str]]) -> tuple[str, ...]:
        """Find required scenarios without a passing case on this profile."""
        if run.suite_environment.name != EVERY_SUITE_ENVIRONMENT:
            return ()
        passed = {
            selector
            for node_id, outcome in outcomes.items()
            if outcome["status"] == "passed"
            for selector in (node_id, node_id.partition("[")[0])
        }
        return tuple(
            selector
            for selector, suites in ContractCoverage.get_selectors().items()
            if selector not in passed and any(fnmatchcase(run.suite, suite) for suite in suites)
        )
