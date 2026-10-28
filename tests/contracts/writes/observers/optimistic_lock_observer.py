from __future__ import annotations

from typing import Any, ClassVar

import pytest

from hare import fields
from hare.exceptions import StaleObjectError
from hare.instrumentation.enums import RowOperation
from hare.models import Model
from tests.contracts.writes.enums import WriteOutcome, WriteRole
from tests.contracts.writes.write_observer import WriteObserver
from tests.contracts.writes.write_path import WritePath
from tests.contracts.writes.write_paths import WRITE_PATHS
from tests.contracts.writes.write_scenario import WriteScenario

LOCKED_ROLES = (WriteRole.SUBJECT, WriteRole.NOTE)
#: The paths writing a row they read before another write changed it - the lock refuses them.
STALE_PATHS = (
    "Model.save, changed row",
    "Model.save, update_fields",
    "Model.delete, soft",
    "Model.hard_delete",
    "QuerySet.bulk_update",
    "Model.delete, soft, cascading to notes",
    "Model.hard_delete, cascading to notes",
    "Model.restore",
)


class OptimisticLockObserver(WriteObserver):
    """The optimistic lock of a model: a path writing a row it read before another write changed it is
    refused with ``StaleObjectError`` and writes nothing; every other write of a row bumps its version once
    and a new row starts at the default."""

    name: ClassVar[str] = "optimistic_lock"
    required_features: ClassVar[tuple[str, ...]] = ("reports_written_row_count",)
    outcomes: ClassVar[dict[str, WriteOutcome]] = {
        path.name: (
            WriteOutcome.REFUSED
            if path.name in STALE_PATHS
            else WriteOutcome.OBSERVED
            if path.get_changed_rows(*LOCKED_ROLES)
            else WriteOutcome.NOT_APPLICABLE
        )
        for path in WRITE_PATHS
    }

    def get_field_attributes(self, role: WriteRole) -> dict[str, Any]:
        return {"version": fields.IntField(db_default=0)} if role in LOCKED_ROLES else {}

    def get_meta_options(self, role: WriteRole) -> dict[str, Any]:
        return {"optimistic_lock_field": "version"} if role in LOCKED_ROLES else {}

    @staticmethod
    async def change_behind_the_path(row: Model) -> None:
        """Writes the row, as another writer would - its version moves on.

        Args:
            row: The row a path has just read.
        """
        if row._meta.optimistic_lock_field:
            await type(row).objects.include_deleted().filter(pk=row.pk).update(name=row.name)

    async def check(self, scenario: WriteScenario, path: WritePath, outcome: WriteOutcome) -> None:
        if outcome is WriteOutcome.REFUSED:
            scenario.after_fetch = self.change_behind_the_path
            before_refusal = await scenario.get_snapshot()
            with pytest.raises(StaleObjectError):
                await path.run(scenario, 5)
            scenario.after_fetch = None
            after_refusal = await scenario.get_snapshot()
            assert self.without_versions(after_refusal) == self.without_versions(before_refusal)
            before = after_refusal
        else:
            before = await scenario.get_snapshot()
        await path.run(scenario, 5)
        after = await scenario.get_snapshot()

        changed_rows = path.get_changed_rows(*LOCKED_ROLES)
        written = {(role.value, pk) for role, _operation, pk in changed_rows}
        for role, operation, pk in changed_rows:
            if operation is RowOperation.DELETE:
                assert pk not in after[role.value]
            elif operation is RowOperation.INSERT:
                assert after[role.value][pk]["version"] == 0, (role, pk)
            else:
                assert after[role.value][pk]["version"] == before[role.value][pk]["version"] + 1, (role, pk)
        for role in LOCKED_ROLES:
            for pk, row in before[role.value].items():
                if (role.value, pk) not in written and pk in after[role.value]:
                    assert after[role.value][pk]["version"] == row["version"], (role, pk)

    @staticmethod
    def without_versions(snapshot: dict[str, dict[Any, dict[str, Any]]]) -> dict[str, dict[Any, dict[str, Any]]]:
        """A snapshot with the versions and the stamps left out.

        Args:
            snapshot: The snapshot.

        Returns:
            Its rows without the ``version`` column.
        """
        return {
            role: {pk: {key: value for key, value in row.items() if key != "version"} for pk, row in rows.items()}
            for role, rows in snapshot.items()
        }
