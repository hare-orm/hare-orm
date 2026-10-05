"""The extensions a model's tables need come from its objects themselves - a field's column type, an
index, a constraint - each answering through the dialect; the migration autodetector and
generate_schemas() ask the same source."""

from __future__ import annotations

import os
import sys
import types

import pytest

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.connections.connections import Connections
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.fields.citext_field import CitextField
from hare.dialects.postgresql.indexes import BloomIndex, GinIndex, GistIndex
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.autodetection.required_extensions import RequiredExtensions
from hare.migrations.operations import CreateExtension, CreateModel
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models import Model
from tests.utils.database_under_test import DatabaseUnderTest

MODULE_NAME = "tests._required_extension_models"


def get_model_state(**options: object) -> ModelState:
    state = State()
    CreateModel(
        name="Reading",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("sensor", fields.IntField()),
            ("tags", fields.JSONField(default=list)),
        ],
        options={"table": "reading", "app": "app", **options},
    ).state_forward("app", state)
    return state.models[("app", "Reading")]


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("index", "extension"),
    [
        pytest.param(BloomIndex(fields=("sensor",), name="reading_bloom"), "bloom", id="bloom_access_method"),
        pytest.param(GistIndex(fields=("sensor",), name="reading_gist"), "btree_gist", id="gist_over_a_scalar"),
        pytest.param(GinIndex(fields=("sensor",), name="reading_gin"), "btree_gin", id="gin_over_a_scalar"),
    ],
)
def test_an_index_names_the_extension_it_needs(index, extension) -> None:
    model_state = get_model_state(indexes=[index])
    assert RequiredExtensions.get_for_model_state(model_state, DialectRegistry.get_dialects()) == {extension}
    assert RequiredExtensions.get_for_model_state(model_state, [DialectRegistry.get_dialect("sqlite")]) == set()


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "index",
    [
        pytest.param(GinIndex(fields=("tags",), name="reading_tags"), id="gin_over_a_type_with_its_own_class"),
        pytest.param(
            GistIndex(fields=("sensor",), name="reading_classed", opclasses=["gist_int4_ops"]),
            id="key_with_an_operator_class",
        ),
    ],
)
def test_an_index_needing_no_extension_names_none(index) -> None:
    model_state = get_model_state(indexes=[index])
    assert RequiredExtensions.get_for_model_state(model_state, DialectRegistry.get_dialects()) == set()


@pytest.mark.database_independent
def test_a_migration_file_creates_what_any_dialect_needs_before_the_model() -> None:
    new_state = State()
    CreateModel(
        name="Note",
        fields=[("id", fields.IntField(primary_key=True)), ("title", CitextField())],
        options={"table": "note", "app": "app", "indexes": [BloomIndex(fields=("id",), name="note_bloom")]},
    ).state_forward("app", new_state)
    operations = OperationGenerator(State(), new_state).generate()
    assert [(type(operation), getattr(operation, "extension_name", None)) for operation in operations] == [
        (CreateExtension, "bloom"),
        (CreateExtension, "citext"),
        (CreateModel, None),
    ]


class ExtensionReading(Model):
    id = fields.IntField(primary_key=True)
    sensor = fields.IntField()

    class Meta:
        app = "models"
        indexes = (
            BloomIndex(fields=("sensor",), name="extension_reading_bloom"),
            GinIndex(fields=("sensor",), name="extension_reading_gin"),
        )


@pytest.mark.asyncio
async def test_generate_schemas_creates_the_extensions_of_the_indexes() -> None:
    """Skips manually - the test opens its own context."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    if DatabaseUnderTest.get_dialect(db_url).name != "postgresql":
        pytest.skip("Capability dialect != postgres")
    module = types.ModuleType(MODULE_NAME)
    module.ExtensionReading = ExtensionReading  # type: ignore[attr-defined]
    sys.modules[MODULE_NAME] = module
    try:
        async with hare_test_context(
            modules=[MODULE_NAME],
            db_url=db_url,
            app_label="models",
            connection_label="models",
            _create_db=True,
            _generate_schemas=False,
        ) as ctx:
            await ctx.generate_schemas()
            connection = Connections.get(next(iter(Connections.current().db_config)))
            _, rows = await connection.execute(
                "SELECT extname FROM pg_extension WHERE extname IN ('bloom', 'btree_gin') ORDER BY extname"
            )
            assert [row["extname"] for row in rows] == ["bloom", "btree_gin"]
            await ExtensionReading.objects.create(id=1, sensor=7)
            assert await ExtensionReading.objects.filter(sensor=7).count() == 1
    finally:
        sys.modules.pop(MODULE_NAME, None)
