"""Q conditions in CheckConstraint.check, UniqueConstraint.condition and PartialIndex.condition."""

from decimal import Decimal

import pytest

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes import PartialIndex
from hare.exceptions import ConfigurationError, IntegrityError, UnSupportedError
from hare.fields.data.json import JSONField
from hare.fields.data.numeric import DecimalField, IntField
from hare.fields.data.text import CharField
from hare.fields.relations.fields import ForeignKeyField
from hare.migrations.operations import RemoveField, RenameField
from hare.migrations.writer import ImportManager, MigrationWriter
from hare.query.expressions import F, Q
from tests.migrations.test_round_trip_real_db import RoundTrip, build_live_state, build_model
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.fixture
def round_trip(db_isolated) -> RoundTrip:
    return RoundTrip(db_isolated.get_connection())


def build_stock_model(table: str, **meta_options):
    return build_model(
        "Stock",
        table,
        {
            "name": CharField(max_length=20, db_index=True),
            "qty": IntField(),
            "price": DecimalField(max_digits=10, decimal_places=2, null=True),
        },
        meta_options,
    )


CONDITIONED_OPTIONS = {
    "constraints": [
        CheckConstraint(check=Q(qty__gte=0) & ~Q(name=""), name="stock_valid"),
        UniqueConstraint(fields=("name",), condition=Q(price__isnull=False) & Q(qty__gt=100), name="stock_big_name"),
    ],
    "indexes": [PartialIndex(fields=["name"], condition=Q(price__gt=Decimal("1")) | Q(qty__lt=3))],
}


async def insert(round_trip: RoundTrip, table: str, row_id: int, name: str, qty: int, price: str | None) -> None:
    price_sql = "NULL" if price is None else price
    await round_trip.connection.execute(
        f'INSERT INTO "{table}" ("id", "name", "qty", "price") VALUES ({row_id}, \'{name}\', {qty}, {price_sql})'
    )


@pytest.mark.asyncio
async def test_q_conditions_are_migrated_and_enforced(round_trip: RoundTrip) -> None:
    table = "q_stock"
    await round_trip.migrate_to(build_stock_model(table))
    conditioned_model = build_stock_model(table, **CONDITIONED_OPTIONS)
    await round_trip.migrate_to(conditioned_model)
    assert round_trip.get_pending_operations(conditioned_model) == []

    await insert(round_trip, table, 1, "a", 5, "2.50")
    with pytest.raises(IntegrityError):
        await insert(round_trip, table, 2, "b", -1, None)
    with pytest.raises(IntegrityError):
        await insert(round_trip, table, 3, "", 1, None)
    await insert(round_trip, table, 4, "big", 200, "1.00")
    if DatabaseUnderTest.get_dialect().features.supports_unique_constraints:
        with pytest.raises(IntegrityError):
            await insert(round_trip, table, 5, "big", 300, "2.00")
    # The partial unique index covers only rows matching its condition.
    await insert(round_trip, table, 6, "big", 300, None)
    await insert(round_trip, table, 7, "big", 50, "3.00")

    assert len(await round_trip.get_plain_index_names(table)) == 2


@pytest.mark.asyncio
async def test_q_conditions_unapply(round_trip: RoundTrip) -> None:
    table = "q_stock_back"
    plain_model = build_stock_model(table)
    await round_trip.migrate_to(plain_model)
    before = round_trip.tracked_state.clone()
    conditioned_model = build_stock_model(table, **CONDITIONED_OPTIONS)
    operations = round_trip.get_pending_operations(conditioned_model)
    await round_trip.apply(operations)
    from hare.migrations.migration import Migration

    migration = Migration(name="0003_back", app_label="models")
    migration.operations = list(operations)
    await migration.unapply(before, dry_run=False, schema_editor=round_trip.editor)
    await insert(round_trip, table, 1, "", -5, None)
    assert len(await round_trip.get_plain_index_names(table)) == 1


@pytest.mark.asyncio
async def test_db_index_and_partial_index_on_the_same_field_stay_two_indexes(round_trip: RoundTrip) -> None:
    table = "q_stock_indexes"
    await round_trip.migrate_to(build_stock_model(table))
    partial_model = build_stock_model(table, indexes=[PartialIndex(fields=["name"], condition=Q(qty=5))])
    operations = await round_trip.migrate_to(partial_model)
    assert [type(operation).__name__ for operation in operations] == ["AddIndex"]
    assert round_trip.get_pending_operations(partial_model) == []
    plain_model = build_stock_model(table)
    await round_trip.migrate_to(plain_model)
    assert round_trip.get_pending_operations(plain_model) == []
    assert len(await round_trip.get_plain_index_names(table)) == 1


def test_q_condition_round_trips_through_a_migration_file() -> None:
    condition = (~Q(qty__lt=0) | Q(price__gte=Decimal("1.50"), name__in=["a", "b"])) & Q(qty__gt=F("min_qty"))
    constraint = CheckConstraint(check=condition, name="stock_check")
    imports = ImportManager()
    code = MigrationWriter.render_value(constraint, imports)
    namespace: dict = {}
    for statement in imports.render():
        exec(statement, namespace)  # noqa: S102 - the writer's own import lines
    assert eval(code, namespace) == constraint  # noqa: S307 - the writer's own rendered code
    assert hash(eval(code, namespace)) == hash(constraint)  # noqa: S307


def test_q_condition_can_not_hold_an_expression_other_than_f() -> None:
    from hare.query.functions import Upper

    with pytest.raises(ConfigurationError, match="can't be written into a migration"):
        MigrationWriter.render_value(Q(name=Upper("other")), ImportManager())


def test_rename_field_renames_q_conditions() -> None:
    model = build_stock_model("q_rename", **CONDITIONED_OPTIONS)
    state = build_live_state(model)
    RenameField(model_name="Stock", old_name="qty", new_name="amount").state_forward("models", state)
    options = state.models[("models", "Stock")].options
    check, unique = options["constraints"]
    assert check.check == Q(amount__gte=0) & ~Q(name="")
    assert unique.condition == Q(price__isnull=False) & Q(amount__gt=100)
    (index,) = [index for index in options["indexes"] if isinstance(index, PartialIndex)]
    assert index.condition == Q(price__gt=Decimal("1")) | Q(amount__lt=3)


def test_remove_field_drops_a_q_check_reading_only_it_and_rejects_one_reading_more() -> None:
    model = build_stock_model(
        "q_remove",
        constraints=[
            CheckConstraint(check=Q(qty__gte=0), name="only_qty"),
            CheckConstraint(check=Q(price__gte=0) | Q(name="x"), name="price_or_name"),
        ],
    )
    state = build_live_state(model)
    RemoveField(model_name="Stock", name="qty").state_forward("models", state)
    assert [constraint.name for constraint in state.models[("models", "Stock")].options["constraints"]] == [
        "price_or_name"
    ]
    with pytest.raises(ConfigurationError, match="price_or_name"):
        RemoveField(model_name="Stock", name="price").state_forward("models", state)


@pytest.mark.asyncio
async def test_condition_reading_a_relation_is_rejected(round_trip: RoundTrip) -> None:
    owner = build_model("Owner", "q_owner", {"name": CharField(max_length=20)})
    item = build_model(
        "Item",
        "q_item",
        {"owner": ForeignKeyField("models.Owner", related_name="items")},
        {"constraints": [CheckConstraint(check=Q(owner__name="a"), name="item_owner")]},
    )
    with pytest.raises(ConfigurationError, match="reads only the model's own columns"):
        await round_trip.migrate_to(owner, item)


def test_empty_condition_is_rejected(db) -> None:
    from tests.testmodels import Tournament

    with pytest.raises(ConfigurationError, match="can't be empty"):
        ConstraintCondition.get_sql(Q(), Tournament, Tournament._meta.connection)


@pytest.mark.asyncio
async def test_sqlite_condition_needing_a_hare_function_is_rejected(round_trip: RoundTrip) -> None:
    model = build_model(
        "Document",
        "q_document",
        {"data": JSONField()},
        {"constraints": [CheckConstraint(check=Q(data__has_key="a"), name="document_has_a")]},
    )
    if round_trip.dialect != "sqlite":
        await round_trip.migrate_to(model)
        return
    with pytest.raises(UnSupportedError, match="only has on hare's own connections"):
        await round_trip.migrate_to(model)


@pytest.mark.asyncio
async def test_generated_schema_enforces_q_conditions_without_drift() -> None:
    import os
    import uuid

    from hare.contrib.test.isolated_contexts import hare_test_context
    from hare.migrations.drift import detect_drift_for_alias

    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    db_url = raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url
    async with hare_test_context(["tests.q_condition_models"], db_url=db_url) as context:
        from tests.q_condition_models import Batch

        await Batch.objects.create(id=1, name="a", qty=5, price=Decimal("2"))
        with pytest.raises(IntegrityError):
            await Batch.objects.create(id=2, name="b", qty=-1)
        await Batch.objects.create(id=3, name="c", qty=200, price=Decimal("1"))
        if context.get_connection().dialect.features.supports_unique_constraints:
            with pytest.raises(IntegrityError):
                await Batch.objects.create(id=4, name="d", qty=200, price=Decimal("5"))
        await Batch.objects.create(id=5, name="e", qty=200)
        apps_config = {"models": {"models": ["tests.q_condition_models"], "default_connection": "default"}}
        result = await detect_drift_for_alias(context.apps, apps_config, "default")
        assert result.operations == []
