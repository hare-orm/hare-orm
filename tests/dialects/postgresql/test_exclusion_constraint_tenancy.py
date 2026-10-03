"""Documents a known, accepted limitation - see docs/soft-delete-versions-tenants/multi-tenancy.md's own
"UniqueConstraint/ExclusionConstraint/unique_together" section. ExclusionConstraint has no
notion of Meta.tenant_field at all, the same way a plain UniqueConstraint doesn't - it checks
only the literal columns named in its own `expressions`. This is not a bug to fix (fixing it
one-sidedly for ExclusionConstraint while leaving UniqueConstraint/unique_together unscoped
would be a worse, inconsistent state) - this test locks in the documented, live behavior, so a
future change can't silently make the two constraint types disagree.
"""

import datetime as dt
import os
import sys
import types

import pytest

from hare import fields
from hare.contrib.test.helpers import hare_test_context
from hare.core.connections import Connections
from hare.ddl.constraints import ExclusionConstraint
from hare.dialects.postgresql.fields.ranges import DateTimeRangeField, Range
from hare.exceptions import IntegrityError
from hare.models import Model
from hare.models.tenancy import Tenancy

MODULE_NAME = "tests._exclusion_constraint_tenancy_models"


class Booking(Model):
    id = fields.IntField(primary_key=True)
    company_id = fields.IntField()
    resource = fields.IntField()
    during = DateTimeRangeField()

    class Meta:
        app = "models"
        tenant_field = "company_id"
        constraints = (
            ExclusionConstraint(
                name="no_overlapping_bookings",
                expressions=(("resource", "="), ("during", "&&")),
            ),
        )


@pytest.mark.asyncio
async def test_exclusion_constraint_without_tenant_column_collides_across_tenants():
    """Confirmed live: two tenants booking the SAME resource for the SAME overlapping time
    range collide, even though the two rows belong to completely independent tenants - because
    `expressions` never named the tenant column itself. This is the exact, documented tradeoff
    - see this module's own docstring.

    Skips manually (instead of via @requires_features) - that decorator's capability check runs
    before the test body, against whatever HareContext happens to already be "current" - since
    this test opens its own context rather than taking the shared `connection`/`db` fixture,
    nothing guarantees one is active yet at that point (mirrors test_inspectdb.py's own
    test_round_trip_composite_foreign_key_model_is_queryable and its identical reasoning)."""
    module = types.ModuleType(MODULE_NAME)
    module.Booking = Booking  # type: ignore[attr-defined]
    sys.modules[MODULE_NAME] = module

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:").replace("\\{", "{").replace("\\}", "}")

    try:
        async with hare_test_context(
            modules=[MODULE_NAME],
            db_url=db_url,
            app_label="models",
            connection_label="models",
            _create_db=True,
            _generate_schemas=False,
        ) as ctx:
            connection = Connections.get(next(iter(Connections.current().db_config)))
            if connection.dialect.name != "postgresql":
                pytest.skip("Capability dialect != postgres")
            # EXCLUDE USING gist needs an operator class for EVERY column it constrains, not
            # just range/geometric ones GiST natively supports - btree_gist supplies one for
            # plain scalar equality (the "resource" INT column here). Extensions are per-
            # database, and `_create_db=True` above created a brand-new, empty database, so this
            # can't be assumed already enabled the way it might be on a long-lived dev database.
            await connection.execute_script("CREATE EXTENSION IF NOT EXISTS btree_gist;")
            await ctx.generate_schemas()

            with Tenancy.scope(1):
                await Booking.objects.create(
                    resource=1,
                    during=Range(
                        lower=dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.UTC),
                        upper=dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.UTC),
                    ),
                )
            with Tenancy.scope(2):
                with pytest.raises(IntegrityError):
                    await Booking.objects.create(
                        resource=1,
                        during=Range(
                            lower=dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.UTC),
                            upper=dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.UTC),
                        ),
                    )
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_exclusion_constraint_with_tenant_column_isolates_tenants():
    """The documented fix from the user's own side: including the tenant column as one of
    `expressions` makes the SAME overlapping booking, for the SAME resource, succeed once per
    tenant - the collision above is entirely a property of what `expressions` names, not
    something hare-orm enforces or blocks."""

    class BookingScoped(Model):
        id = fields.IntField(primary_key=True)
        company_id = fields.IntField()
        resource = fields.IntField()
        during = DateTimeRangeField()

        class Meta:
            app = "models"
            tenant_field = "company_id"
            constraints = (
                ExclusionConstraint(
                    name="no_overlapping_bookings_scoped",
                    expressions=(("company_id", "="), ("resource", "="), ("during", "&&")),
                ),
            )

    module = types.ModuleType(MODULE_NAME)
    module.BookingScoped = BookingScoped  # type: ignore[attr-defined]
    sys.modules[MODULE_NAME] = module

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:").replace("\\{", "{").replace("\\}", "}")

    try:
        async with hare_test_context(
            modules=[MODULE_NAME],
            db_url=db_url,
            app_label="models",
            connection_label="models",
            _create_db=True,
            _generate_schemas=False,
        ) as ctx:
            connection = Connections.get(next(iter(Connections.current().db_config)))
            if connection.dialect.name != "postgresql":
                pytest.skip("Capability dialect != postgres")
            await connection.execute_script("CREATE EXTENSION IF NOT EXISTS btree_gist;")
            await ctx.generate_schemas()

            with Tenancy.scope(1):
                await BookingScoped.objects.create(
                    resource=1,
                    during=Range(
                        lower=dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.UTC),
                        upper=dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.UTC),
                    ),
                )
            with Tenancy.scope(2):
                await BookingScoped.objects.create(
                    resource=1,
                    during=Range(
                        lower=dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.UTC),
                        upper=dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.UTC),
                    ),
                )
    finally:
        sys.modules.pop(MODULE_NAME, None)
