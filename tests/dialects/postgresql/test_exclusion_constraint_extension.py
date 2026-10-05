"""A GiST ExclusionConstraint over a plain scalar column (`("team", "=")`) needs the btree_gist
extension - hare creates it the way it creates citext/vector for their fields."""

import datetime as dt
import os
import sys
import types

import pytest

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.connections.connections import Connections
from hare.ddl.constraints import ExclusionConstraint
from hare.ddl.enums import ExclusionConstraintUsing
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.fields.ranges import DateTimeRangeField, IntRangeField, Range
from hare.exceptions import IntegrityError
from hare.models import Model

MODULE_NAME = "tests._exclusion_constraint_extension_models"


class ExtensionTeam(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        app = "models"


class ExtensionEvent(Model):
    id = fields.IntField(primary_key=True)
    team = fields.ForeignKeyField("models.ExtensionTeam")
    during = DateTimeRangeField()

    class Meta:
        app = "models"
        constraints = (
            ExclusionConstraint(
                name="no_overlapping_extension_events",
                expressions=(("team", "="), ("during", "&&")),
                using=ExclusionConstraintUsing.GIST,
            ),
        )


def test_gist_exclusion_over_a_scalar_column_requires_btree_gist():
    fields_by_name = {
        "team": ExtensionEvent._meta.fields_map["team"],
        "during": DateTimeRangeField(),
        "label": fields.CharField(max_length=10),
        "span": IntRangeField(),
    }

    def required_extension(*expressions, using=ExclusionConstraintUsing.GIST):
        return POSTGRESQL_DIALECT.schema_editor_class.constraint_statements_class.get_exclusion_constraint_extension(
            ExclusionConstraint(name="c", expressions=expressions, using=using), fields_by_name
        )

    assert required_extension(("team", "="), ("during", "&&")) == "btree_gist"
    assert required_extension(("label", "="), ("span", "&&")) == "btree_gist"
    assert required_extension(("during", "&&"), ("span", "&&")) is None
    assert required_extension(("team", "="), using=ExclusionConstraintUsing.BTREE) is None
    assert required_extension(("team", "="), using=ExclusionConstraintUsing.SPGIST) is None


@pytest.mark.asyncio
async def test_gist_exclusion_over_a_scalar_column_is_created_without_declaring_btree_gist():
    """The documented `(("team", "="), ("during", "&&"))` example failed with "data type integer
    has no default operator class for access method gist" unless btree_gist was enabled by hand.

    Skips manually - the test opens its own context (see the tenancy exclusion tests)."""
    module = types.ModuleType(MODULE_NAME)
    module.ExtensionTeam = ExtensionTeam  # type: ignore[attr-defined]
    module.ExtensionEvent = ExtensionEvent  # type: ignore[attr-defined]
    sys.modules[MODULE_NAME] = module

    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:").replace("\\{", "{").replace("\\}", "}")

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
            await ctx.generate_schemas()

            team = await ExtensionTeam.objects.create(id=1)
            start = dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.UTC)
            await ExtensionEvent.objects.create(team=team, during=Range(start, start + dt.timedelta(hours=2)))
            with pytest.raises(IntegrityError):
                await ExtensionEvent.objects.create(
                    team=team, during=Range(start + dt.timedelta(hours=1), start + dt.timedelta(hours=3))
                )
    finally:
        sys.modules.pop(MODULE_NAME, None)
