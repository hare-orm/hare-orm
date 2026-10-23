"""An exclusion constraint over an expression of hare: written in PostgreSQL's SQL, enforced, read back as it
is created (no drift, no second migration), and renamed with the field it reads by RenameField."""

import pytest

from hare import fields
from hare.ddl.constraints import ExclusionConstraint
from hare.ddl.enums import ExclusionConstraintUsing
from hare.exceptions import IntegrityError
from hare.migrations.drift import detect_drift
from hare.query.functions import Lower
from tests.migrations.round_trips.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model


def build_coded_widget(field_name: str):
    return build_model(
        "CodedWidget",
        "coded_widget",
        {field_name: fields.CharField(max_length=20)},
        {
            "constraints": [
                ExclusionConstraint(
                    name="excl_coded_widget_code",
                    expressions=((Lower(field_name), "="),),
                    using=ExclusionConstraintUsing.HASH,
                )
            ]
        },
    )


@pytest.mark.asyncio
async def test_an_exclusion_constraint_over_an_expression_of_hare(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect != "postgresql":
        pytest.skip("EXCLUDE is PostgreSQL's")
    widget = build_coded_widget("code")
    await round_trip.migrate_to(widget)
    connection = round_trip.connection

    definitions = await connection.execute_dicts(
        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conname = 'excl_coded_widget_code'"
    )
    assert definitions == [{"definition": "EXCLUDE USING hash (lower((code)::text) WITH =)"}]
    await connection.execute_script("INSERT INTO coded_widget (id, code) VALUES (1, 'A')")
    with pytest.raises(IntegrityError):
        await connection.execute_script("INSERT INTO coded_widget (id, code) VALUES (2, 'a')")
    assert (await detect_drift(connection, build_live_state(widget), [APP_LABEL])).operations == []
    assert round_trip.get_pending_operations(widget) == []

    renamed_widget = build_coded_widget("label")
    operations = await round_trip.migrate_to(renamed_widget, renames={"models.CodedWidget.code": "label"})

    assert [type(operation).__name__ for operation in operations] == ["RenameField"]
    assert round_trip.get_pending_operations(renamed_widget) == []
    with pytest.raises(IntegrityError):
        await connection.execute_script("INSERT INTO coded_widget (id, label) VALUES (3, 'a')")
