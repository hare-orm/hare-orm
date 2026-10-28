from __future__ import annotations

import contextlib
from collections.abc import Generator
from typing import Any, ClassVar

from hare import fields
from hare.exceptions import DoesNotExist, IntegrityError, QueryError, ValidationError
from hare.models.tenancy.tenancy import Tenancy
from tests.contracts.writes.enums import WriteOutcome, WriteRole
from tests.contracts.writes.write_observer import WriteObserver
from tests.contracts.writes.write_path import WritePath
from tests.contracts.writes.write_paths import WRITE_PATHS
from tests.contracts.writes.write_scenario import WriteScenario

SCOPED_ROLES = (WriteRole.SUBJECT, WriteRole.NOTE)
#: The tenant of the starting rows, and the one whose scope tries to write them.
OWNER_TENANT = 1
FOREIGN_TENANT = 2


class TenantScopeObserver(WriteObserver):
    """The scope of a tenant: a path run in a scope gives the rows it inserts that tenant and never leaves a
    row of another; run in the scope of another tenant, it changes none of the first tenant's rows."""

    name: ClassVar[str] = "tenant_scope"
    outcomes: ClassVar[dict[str, WriteOutcome]] = {
        path.name: WriteOutcome.OBSERVED if path.get_changed_rows(*SCOPED_ROLES) else WriteOutcome.NOT_APPLICABLE
        for path in WRITE_PATHS
    }

    def get_field_attributes(self, role: WriteRole) -> dict[str, Any]:
        return {"tenant_id": fields.IntField()} if role in SCOPED_ROLES else {}

    def get_meta_options(self, role: WriteRole) -> dict[str, Any]:
        return {"tenant_field": "tenant_id"} if role in SCOPED_ROLES else {}

    @contextlib.contextmanager
    def get_scope(self) -> Generator[None]:
        with Tenancy.scope(OWNER_TENANT):
            yield

    async def check(self, scenario: WriteScenario, path: WritePath, outcome: WriteOutcome) -> None:
        if outcome is WriteOutcome.OBSERVED:
            await self.check_a_foreign_scope_writes_nothing_of_the_owner(scenario, path)
        with Tenancy.scope(OWNER_TENANT):
            await path.run(scenario, 5)
        after = await scenario.get_snapshot()
        for role in SCOPED_ROLES:
            assert {row["tenant_id"] for row in after[role.value].values()} <= {OWNER_TENANT}, role

    async def check_a_foreign_scope_writes_nothing_of_the_owner(
        self, scenario: WriteScenario, path: WritePath
    ) -> None:
        """Runs the path in the scope of another tenant: the first tenant's rows stay as they were.

        Args:
            scenario: The scenario.
            path: The write path.
        """
        before = await scenario.get_snapshot()
        with (
            Tenancy.scope(FOREIGN_TENANT),
            contextlib.suppress(QueryError, DoesNotExist, IntegrityError, ValidationError),
        ):
            await path.run(scenario, 5)
        after = await scenario.get_snapshot()
        for role in SCOPED_ROLES:
            for pk, row in before[role.value].items():
                assert after[role.value].get(pk) == row, (role, pk)
        # The foreign tenant's rows are left out of the run that follows.
        for model in (scenario.note, scenario.subject):
            await model.objects.all_tenants().filter(tenant_id=FOREIGN_TENANT).hard_delete()
