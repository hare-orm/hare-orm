from __future__ import annotations

from typing import Any, ClassVar

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import NoValuesFetched
from hare.instrumentation.enums import RowOperation
from tests.contracts.writes.enums import WriteOutcome, WriteRole
from tests.contracts.writes.write_observer import WriteObserver
from tests.contracts.writes.write_path import WritePath
from tests.contracts.writes.write_paths import WRITE_PATHS
from tests.contracts.writes.write_scenario import WriteScenario

GENERATED_ROLES = (WriteRole.SUBJECT, WriteRole.NOTE)


class GeneratedFieldObserver(WriteObserver):
    """A ``GeneratedField``: the database computes it from the amount of each row a path inserts or updates,
    and no path leaves a row holding a value that isn't its amount's."""

    name: ClassVar[str] = "generated_field"
    outcomes: ClassVar[dict[str, WriteOutcome]] = {
        path.name: WriteOutcome.OBSERVED if path.get_changed_rows(*GENERATED_ROLES) else WriteOutcome.NOT_APPLICABLE
        for path in WRITE_PATHS
    }

    def get_field_attributes(self, role: WriteRole) -> dict[str, Any]:
        if role not in GENERATED_ROLES:
            return {}
        return {"doubled": fields.GeneratedField(expression=RawSQLTerm("amount * 2"), output_field=fields.IntField())}

    async def check(self, scenario: WriteScenario, path: WritePath, outcome: WriteOutcome) -> None:
        fetched = []

        async def remember(obj):
            fetched.append(obj)

        scenario.after_fetch = remember
        await path.run(scenario, 5)
        after = await scenario.get_snapshot()
        for role in GENERATED_ROLES:
            for pk, row in after[role.value].items():
                assert row["doubled"] == row["amount"] * 2, (role, pk)
        for role, operation, pk in path.get_changed_rows(*GENERATED_ROLES):
            if operation is not RowOperation.DELETE:
                assert after[role.value][pk]["doubled"] is not None
                for obj in fetched:
                    if type(obj) is not scenario.models.get_model(role) or obj.pk != pk:
                        continue
                    try:
                        value = obj.doubled
                    except NoValuesFetched:
                        continue
                    assert value == after[role.value][pk]["amount"] * 2
