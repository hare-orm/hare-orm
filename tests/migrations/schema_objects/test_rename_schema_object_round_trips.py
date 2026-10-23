"""A renamed constraint or index goes back to its old name when the migration is unapplied."""

import pytest

from hare.ddl.constraints import CheckConstraint
from hare.ddl.indexes.index import Index
from hare.fields.data.numeric import IntField
from hare.migrations import operations as ops
from hare.migrations.drift import detect_drift
from hare.migrations.migration import Migration
from hare.query.expressions import Q
from tests.migrations.round_trips.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("meta_options", "rename"),
    [
        (
            {"constraints": [CheckConstraint(check=Q(rank__gte=0), name="ck_rank_old")]},
            ops.RenameConstraint(model_name="Ranked", old_name="ck_rank_old", new_name="ck_rank_new"),
        ),
        (
            {"indexes": [Index(fields=("rank",), name="ix_rank_old")]},
            ops.RenameIndex(model_name="Ranked", new_name="ix_rank_new", old_name="ix_rank_old"),
        ),
    ],
)
async def test_unapplying_a_rename_restores_the_old_name(db_isolated_no_schema, meta_options, rename):
    """SQLite rebuilt the table from the renamed model going back - the constraint kept its new name."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    model = build_model("Ranked", "renamed_objects", {"rank": IntField()}, meta_options)
    await round_trip.migrate_to(model)
    before = round_trip.tracked_state.clone()
    migration = Migration(name="0099_rename", app_label=APP_LABEL)
    migration.operations = [rename]
    after = await migration.apply(before.clone(), schema_editor=round_trip.editor)
    assert (await detect_drift(round_trip.connection, after, [APP_LABEL])).operations == []
    await migration.unapply(before.clone(), schema_editor=round_trip.editor)
    assert (await detect_drift(round_trip.connection, build_live_state(model), [APP_LABEL])).operations == []
