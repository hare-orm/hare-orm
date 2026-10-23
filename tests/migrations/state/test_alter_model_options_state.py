"""A hand-written AlterModelOptions leaves the migration state as the model's own."""

import pytest

from hare.fields.data.numeric import IntField
from hare.migrations import operations as ops
from tests.migrations.round_trips.test_round_trip_real_db import RoundTrip, build_model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options", [{}, {"ordering": []}, {"ordering": ["id"]}, {"table_description": ""}, {"managed": True}]
)
async def test_options_the_state_doesnt_keep_leave_no_drift(db_isolated_no_schema, options):
    """An empty option, managed=True or an option the state never keeps (ordering) were stored - the
    model always seemed to have changed options."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    model = build_model("Opt", "options_state", {"rank": IntField()}, options)
    await round_trip.migrate_to(model)
    await round_trip.apply([ops.AlterModelOptions("Opt", options)])
    assert round_trip.get_pending_operations(model) == []
