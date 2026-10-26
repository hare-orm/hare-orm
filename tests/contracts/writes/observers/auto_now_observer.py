from __future__ import annotations

from typing import Any, ClassVar

from hare import fields
from hare.instrumentation.enums import RowOperation
from tests.contracts.writes.enums import WriteOutcome, WriteRole
from tests.contracts.writes.write_observer import WriteObserver
from tests.contracts.writes.write_path import WritePath
from tests.contracts.writes.write_paths import WRITE_PATHS
from tests.contracts.writes.write_scenario import WriteScenario

STAMPED_ROLES = (WriteRole.SUBJECT, WriteRole.NOTE)


class AutoNowObserver(WriteObserver):
    """The ``auto_now`` and ``auto_now_add`` fields: a path inserting a row stamps both, a path updating one
    advances ``auto_now`` and keeps ``auto_now_add``, a row no path writes keeps its stamps."""

    name: ClassVar[str] = "auto_now"
    outcomes: ClassVar[dict[str, WriteOutcome]] = {
        path.name: WriteOutcome.OBSERVED if path.get_changed_rows(*STAMPED_ROLES) else WriteOutcome.NOT_APPLICABLE
        for path in WRITE_PATHS
    }

    def get_field_attributes(self, role: WriteRole) -> dict[str, Any]:
        if role not in STAMPED_ROLES:
            return {}
        return {
            "touched_at": fields.DatetimeField(auto_now=True, null=True),
            "created_at": fields.DatetimeField(auto_now_add=True, null=True),
        }

    async def check(self, scenario: WriteScenario, path: WritePath, outcome: WriteOutcome) -> None:
        before = await scenario.get_snapshot()
        await path.run(scenario, 5)
        after = await scenario.get_snapshot()

        changed_rows = path.get_changed_rows(*STAMPED_ROLES)
        written = {(role.value, pk) for role, _operation, pk in changed_rows}
        for role, operation, pk in changed_rows:
            if operation is RowOperation.DELETE:
                assert pk not in after[role.value]
                continue
            row = after[role.value][pk]
            if operation is RowOperation.INSERT:
                assert row["touched_at"] is not None
                assert row["created_at"] is not None
            elif operation is RowOperation.UPDATE:
                old_row = before[role.value][pk]
                assert row["touched_at"] > old_row["touched_at"], (role, pk)
                assert row["created_at"] == old_row["created_at"], (role, pk)
        for role in STAMPED_ROLES:
            for pk, row in before[role.value].items():
                if (role.value, pk) not in written and pk in after[role.value]:
                    assert after[role.value][pk]["touched_at"] == row["touched_at"], (role, pk)
                    assert after[role.value][pk]["created_at"] == row["created_at"], (role, pk)
