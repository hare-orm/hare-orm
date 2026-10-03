import os
from decimal import Decimal

import pytest

from hare.contrib.test import requires_features
from hare.dialects.registry import DialectRegistry
from hare.exceptions import (
    ConfigurationError,
    QueryError,
    StaleObjectError,
    UnSupportedError,
)
from hare.fields import DecimalField, IntField, TextField
from hare.fields.generated import GeneratedField
from hare.query.functions import Avg
from tests.fields.models_generated_field import (
    AutoReturningPricedItem,
    AveragedQuantityItem,
    DbDefaultDoubledItem,
    GeneratedFieldFkTargetStockItem,
    GeneratedFieldFkTargetWarehouse,
    LabeledItem,
    PricedItem,
    TimestampedItem,
    VersionedPricedItem,
)
from tests.fields.models_generated_field_virtual import VirtualWidget
from tests.utils.timezone_context import override_timezone


def test_string_expression_used_verbatim_on_both_dialects():
    field = GeneratedField(expression="price * quantity", output_field=IntField())
    expected = "GENERATED ALWAYS AS (price * quantity) STORED"
    assert field.get_generated_sql(DialectRegistry.get_dialect("sqlite")) == expected
    assert field.get_generated_sql(DialectRegistry.get_dialect("postgresql")) == expected


def test_dict_expression_resolves_per_dialect():
    field = GeneratedField(
        expression={"sqlite": "CAST(a AS TEXT) || b", "postgresql": "a::text || b"},
        output_field=TextField(),
    )
    sqlite_sql = field.get_generated_sql(DialectRegistry.get_dialect("sqlite"))
    assert sqlite_sql == "GENERATED ALWAYS AS (CAST(a AS TEXT) || b) STORED"
    assert (
        field.get_generated_sql(DialectRegistry.get_dialect("postgresql"))
        == "GENERATED ALWAYS AS (a::text || b) STORED"
    )


def test_dict_expression_missing_dialect_raises():
    field = GeneratedField(expression={"sqlite": "a || b"}, output_field=TextField())
    field.model_field_name = "combined"
    with pytest.raises(ConfigurationError, match="no expression for dialect 'postgresql'"):
        field.get_generated_sql(DialectRegistry.get_dialect("postgresql"))


def test_stored_false_on_postgres_raises():
    field = GeneratedField(expression="a * b", output_field=IntField(), stored=False)
    field.model_field_name = "area"
    with pytest.raises(UnSupportedError, match="postgresql dialect only supports STORED"):
        field.get_generated_sql(DialectRegistry.get_dialect("postgresql"))


def test_stored_false_on_sqlite_uses_virtual():
    field = GeneratedField(expression="a * b", output_field=IntField(), stored=False)
    assert field.get_generated_sql(DialectRegistry.get_dialect("sqlite")) == "GENERATED ALWAYS AS (a * b) VIRTUAL"


def test_validators_kwarg_rejected():
    """The database computes a GeneratedField's value - the application never assigns one, so
    Field.validate() (which checks self.validators) is never called; to_db_value() delegates
    entirely to output_field.to_db_value() instead. A validators= passed directly here used to
    be silently accepted and then silently never run at all."""
    from hare.fields.validators import MaxValueValidator

    with pytest.raises(ConfigurationError, match="validators"):
        GeneratedField(expression="a + b", output_field=IntField(), validators=[MaxValueValidator(10)])


@pytest.mark.parametrize("kwarg_name", ["primary_key", "pk"])
def test_primary_key_kwarg_rejected(kwarg_name):
    """allows_generated=True lets a bare-declared auto-increment integer PK
    (IntField(primary_key=True), whose GENERATED_SQL is a self-contained "SERIAL NOT NULL
    PRIMARY KEY"-style string) pass ModelMeta's own custom-pk guard - but that guard can't tell
    that guarantee apart from THIS class's own GENERATED_SQL, which is just the bare generation
    clause with no type prefix and no primary-key marker at all. Confirmed live before this fix:
    GeneratedField(primary_key=True) raised a raw Postgres syntax error at CREATE TABLE time
    ("near ALWAYS", no column type before GENERATED ALWAYS AS) and produced a SQLite table with
    no PRIMARY KEY constraint at all - looked accepted at class-definition time, guaranteed to
    break (loudly on Postgres, silently on SQLite) on the very next step."""
    with pytest.raises(ConfigurationError, match="primary_key"):
        GeneratedField(expression="a + b", output_field=IntField(), **{kwarg_name: True})


def test_sql_type_delegates_to_output_field_per_dialect():
    """DecimalField's own SQL_TYPE differs by dialect (VARCHAR(40) on sqlite, DECIMAL(m,d) on
    postgres) - GeneratedField must resolve through output_field for each dialect, not just
    once at construction time."""
    field = GeneratedField(expression="a * b", output_field=DecimalField(max_digits=5, decimal_places=2))
    assert field.get_column_type(DialectRegistry.get_dialect("sqlite")) == "VARCHAR(40)"
    assert field.get_column_type(DialectRegistry.get_dialect("postgresql")) == "DECIMAL(5,2)"


def test_python_type_delegates_to_output_field():
    field = GeneratedField(expression="a * b", output_field=DecimalField(max_digits=5, decimal_places=2))
    assert field.get_python_type() is Decimal


def test_deconstruct_round_trips_without_duplicate_generated_kwarg():
    """generated=True is implied by GeneratedField itself (hardcoded in __init__) - deconstruct()
    must not also emit it as a kwarg, or replaying it via GeneratedField(**kwargs) (what a
    generated migration file does) collides with the hardcoded one (TypeError: got multiple
    values for keyword argument 'generated')."""
    field = GeneratedField(expression="a + b", output_field=IntField())
    path, args, kwargs = field.deconstruct()
    assert "generated" not in kwargs
    reconstructed = GeneratedField(*args, **kwargs)
    assert reconstructed.expression == "a + b"
    assert reconstructed.generated is True


@pytest.mark.asyncio
async def test_generated_field_stored_decimal_expression(db_generated_field):
    """A STORED GeneratedField computing price * quantity - dialect-agnostic, runs on whatever
    HARE_TEST_DB is configured for."""
    item = await PricedItem.objects.create(price=Decimal("19.99"), quantity=3)
    fetched = await PricedItem.objects.get(id=item.id)
    assert fetched.total == Decimal("59.97")


@pytest.mark.asyncio
async def test_generated_decimal_column_compares_and_orders_as_a_decimal(db_generated_field):
    """SQLite stores the computed value as text - it compares and orders the way a DecimalField
    column does, not as text."""
    fifteen = await PricedItem.objects.create(price=Decimal("1.50"), quantity=10)
    twenty_two = await PricedItem.objects.create(price=Decimal("2.25"), quantity=10)
    three = await PricedItem.objects.create(price=Decimal("1.50"), quantity=2)

    def ids(**filters):
        return PricedItem.objects.filter(**filters).order_by("id").values_list("id", flat=True)

    assert await ids(total=Decimal("15")) == [fifteen.id]
    assert await ids(total__gte=Decimal("15")) == [fifteen.id, twenty_two.id]
    assert await ids(total__in=[Decimal("15.00"), Decimal("3")]) == [fifteen.id, three.id]
    assert await ids(total__lt=20) == [fifteen.id, three.id]
    assert await PricedItem.objects.all().order_by("-total").values_list("id", flat=True) == [
        twenty_two.id,
        fifteen.id,
        three.id,
    ]


@pytest.mark.asyncio
async def test_generated_field_populated_in_memory_without_a_refetch(db_generated_field):
    """create()'s own RETURNING (Postgres/rust_pg's own executor override always asks for it;
    SQLite used to only ever ask for last_insert_rowid(), never a RETURNING row, so a
    GeneratedField stayed unset in memory - None, not the real computed value - until a separate
    re-fetch) must populate a GeneratedField onto the just-created instance directly, identically
    on every backend - this is the exact gap the sibling re-fetching test above doesn't catch."""
    item = await PricedItem.objects.create(price=Decimal("19.99"), quantity=3)
    assert item.total == Decimal("59.97")


@pytest.mark.asyncio
async def test_save_with_explicit_pk_does_not_pull_an_unrelated_generated_field_into_the_insert(
    db_generated_field,
):
    """_prepare_insert_columns(include_generated=True) exists so a caller-supplied custom pk
    value on an otherwise auto-increment pk still lands in the INSERT column list - id's own
    field.generated=True is set implicitly by IntField's __init__ for exactly that reason. A REAL
    GeneratedField (total, computed from price*quantity) also sets field.generated=True, and
    field.generated alone can't tell the two apart - a model combining a custom-settable pk with
    an unrelated GeneratedField used to pull that other column into the insert list too, failing
    validation instead of being correctly omitted (GeneratedField.to_db_value() requires a real
    numeric value, which an omitted field never has)."""
    item = PricedItem(id=999001, price=Decimal("10.00"), quantity=2)
    await item.save()

    fetched = await PricedItem.objects.get(id=999001)
    assert fetched.total == Decimal("20.00")


@pytest.mark.asyncio
async def test_bulk_create_with_explicit_pk_does_not_pull_an_unrelated_generated_field_into_the_insert(
    db_generated_field,
):
    """Same gap as test_save_with_explicit_pk_does_not_pull_an_unrelated_generated_field_into_the_insert
    above, reached through bulk_create()'s own _execute_many() instead of save()'s single-row
    insert - both share the same regular_columns_all/_prepare_insert_columns() precomputation."""
    await PricedItem.objects.bulk_create([PricedItem(id=999002, price=Decimal("15.00"), quantity=4)])

    fetched = await PricedItem.objects.get(id=999002)
    assert fetched.total == Decimal("60.00")


@pytest.mark.asyncio
async def test_create_and_save_with_explicit_pk_read_back_the_generated_field(db_generated_field):
    """An INSERT carrying a caller-supplied pk asked RETURNING for nothing, leaving the
    GeneratedField unset in memory until a re-fetch."""
    created = await PricedItem.objects.create(id=999003, price=Decimal("3.00"), quantity=3)
    saved = PricedItem(id=999004, price=Decimal("5.00"), quantity=2)
    await saved.save()
    defaulted = await DbDefaultDoubledItem.objects.create(id=999005)

    assert created.total == Decimal("9.00")
    assert saved.total == Decimal("10.00")
    assert (defaulted.quantity, defaulted.doubled) == (4, 8)
    assert created._custom_generated_pk is True


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_populates_generated_field_for_explicit_pk_objects(db_generated_field):
    items = [PricedItem(id=999006, price=Decimal("2.00"), quantity=7), PricedItem(price=Decimal("1.00"), quantity=2)]

    await PricedItem.objects.bulk_create(items, returning=True)

    assert [(item.id is not None, item.total) for item in items] == [(True, Decimal("14.00")), (True, Decimal("2.00"))]
    assert items[0].id == 999006


@pytest.mark.asyncio
async def test_generated_field_stored_dict_expression(db_generated_field):
    """A STORED GeneratedField with per-dialect expression syntax (Postgres's `::text` cast
    has no SQLite equivalent)."""
    item = await LabeledItem.objects.create(quantity=7)
    fetched = await LabeledItem.objects.get(id=item.id)
    assert fetched.label == "Item #7"


@pytest.mark.asyncio
async def test_generated_field_virtual(db_generated_field_virtual):
    """A stored=False (VIRTUAL) GeneratedField, recomputed on read rather than materialized -
    SQLite only."""
    widget = await VirtualWidget.objects.create(length=4, width=5)
    fetched = await VirtualWidget.objects.get(id=widget.id)
    assert fetched.area == 20


@pytest.mark.asyncio
async def test_save_update_refreshes_the_generated_field_in_memory(db_generated_field):
    """save()'s UPDATE path silently left a GeneratedField at its stale pre-update in-memory
    value (only INSERT ever refreshed it, via RETURNING) - a change to one of the expression's
    own input columns (quantity here) never showed up on `total` without a separate re-fetch."""
    item = await PricedItem.objects.create(price=Decimal("10.00"), quantity=2)
    assert item.total == Decimal("20.00")

    item.quantity = 5
    await item.save()

    assert item.total == Decimal("50.00")
    assert (await PricedItem.objects.get(id=item.id)).total == Decimal("50.00")


@pytest.mark.asyncio
async def test_directly_assigning_a_generated_field_is_overwritten_by_the_next_save(db_generated_field):
    """Assigning straight to a GeneratedField is silently excluded from the UPDATE's own SET
    clause (a generated column can never be written) - update_or_create(defaults={"total": ...})
    hits exactly this path via update_from_dict(). The RETURNING-based refresh corrects the
    misleading in-memory value back to what the database actually computed."""
    item = await PricedItem.objects.create(price=Decimal("10.00"), quantity=2)

    item, created = await PricedItem.objects.update_or_create(id=item.id, defaults={"total": Decimal("999.00")})

    assert created is False
    assert item.total == Decimal("20.00")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_populates_generated_field(db_generated_field):
    """bulk_create(returning=True) used to bring back only the pk, silently leaving a
    GeneratedField at its pre-insert Python-side value (None) forever - the object's real,
    DB-computed `total` was written correctly, just never read back into Python. Now the
    RETURNING clause asks for generated_db_fields too, matching what a plain create() already
    does via _process_insert_result()."""
    items = [
        PricedItem(price=Decimal("2.50"), quantity=3),
        PricedItem(price=Decimal("4.00"), quantity=2),
    ]
    await PricedItem.objects.bulk_create(items, returning=True)

    assert all(item.id is not None for item in items)
    assert items[0].total == Decimal("7.50")
    assert items[1].total == Decimal("8.00")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_returning_false_leaves_generated_field_untouched(db_generated_field):
    """The default (returning=False) contract must stay exactly as before - `total` stays
    unset on the Python side even though the database computed and stored a real value for it."""
    items = [PricedItem(price=Decimal("1.00"), quantity=1)]
    await PricedItem.objects.bulk_create(items)

    assert items[0].id is None
    assert items[0].total is None


@pytest.mark.asyncio
async def test_bulk_update_returning_populates_fresh_value(db_generated_field):
    """bulk_update(returning=True) brings back each GeneratedField's fresh,
    server-recomputed value onto the object that triggered it - unlike bulk_create()'s own
    returning (Postgres-only, matched by row POSITION), this is matched by PK VALUE, so it
    works on every dialect RETURNING supports, sqlite included."""
    created = [
        await PricedItem.objects.create(price=Decimal("2.50"), quantity=3),
        await PricedItem.objects.create(price=Decimal("4.00"), quantity=2),
    ]
    # Re-fetch first - a plain create() isn't guaranteed to populate a GeneratedField in memory
    # (a separate, pre-existing gap unrelated to this test's own subject).
    items = [await PricedItem.objects.get(id=item.id) for item in created]
    assert items[0].total == Decimal("7.50")
    assert items[1].total == Decimal("8.00")

    items[0].quantity = 10
    items[1].quantity = 5
    count = await PricedItem.objects.bulk_update(items, fields=["quantity"], returning=True)

    assert count == 2
    assert items[0].total == Decimal("25.00")
    assert items[1].total == Decimal("20.00")
    refreshed = await PricedItem.objects.get(id=items[0].id)
    assert refreshed.total == Decimal("25.00")


@pytest.mark.asyncio
async def test_bulk_update_rejects_generated_field_in_fields(db_generated_field):
    """QuerySet.update() rejects a GeneratedField target with a clean IntegrityError before ever
    reaching the database - bulk_update() builds its own SET clause directly and used to have no
    equivalent guard, letting the same mistake reach the database and surface as a raw,
    dialect-leaking driver error (sqlite: "cannot UPDATE generated column ...") instead."""
    item = await PricedItem.objects.create(price=Decimal("2.50"), quantity=3)
    item.total = Decimal("999.99")

    with pytest.raises(QueryError, match="total"):
        await PricedItem.objects.bulk_update([item], fields=["total"])


@pytest.mark.asyncio
async def test_bulk_update_returning_false_leaves_value_stale(db_generated_field):
    """The default (returning=False) contract is unchanged - the in-memory
    object keeps whatever GeneratedField value it had before the update, even though the
    database recomputed a new one."""
    created = await PricedItem.objects.create(price=Decimal("1.00"), quantity=1)
    item = await PricedItem.objects.get(id=created.id)
    assert item.total == Decimal("1.00")

    item.quantity = 99
    await PricedItem.objects.bulk_update([item], fields=["quantity"])

    assert item.total == Decimal("1.00")
    refreshed = await PricedItem.objects.get(id=item.id)
    assert refreshed.total == Decimal("99.00")


@pytest.mark.asyncio
async def test_bulk_update_returning_no_generated_field_is_a_noop(db_generated_field):
    """A model with no GeneratedField at all has nothing for RETURNING to bring back - the flag
    must not add an empty/useless RETURNING clause or otherwise change behavior."""
    item = await LabeledItem.objects.create(quantity=1)
    item.quantity = 2
    count = await LabeledItem.objects.bulk_update([item], fields=["quantity"], returning=True)
    assert count == 1
    refreshed = await LabeledItem.objects.get(id=item.id)
    assert refreshed.label == "Item #2"


@pytest.mark.asyncio
async def test_bulk_update_returning_populates_non_stale_object_despite_batch_failure(
    db_generated_field,
):
    """A batch mixing one stale and one fresh object still raises StaleObjectError for the whole
    batch (existing count-based contract, unchanged) - but the fresh object's GeneratedField must
    still be populated, mirroring how its version/dirty-snapshot are already bumped before the
    raise (objects that DID get written must not be penalized for a sibling's staleness)."""
    created = [
        await VersionedPricedItem.objects.create(price=Decimal("2.50"), quantity=3),
        await VersionedPricedItem.objects.create(price=Decimal("4.00"), quantity=2),
    ]
    items = [await VersionedPricedItem.objects.get(id=item.id) for item in created]

    # Make items[1] stale behind our back - save() bumps its DB version to 1, items[1] in-memory
    # still thinks it's 0.
    stale_target = await VersionedPricedItem.objects.get(id=items[1].id)
    stale_target.quantity = 999
    await stale_target.save()

    items[0].quantity = 10
    items[1].quantity = 5
    with pytest.raises(StaleObjectError):
        await VersionedPricedItem.objects.bulk_update(items, fields=["quantity"], returning=True)

    assert items[0].total == Decimal("25.00")
    refreshed = await VersionedPricedItem.objects.get(id=items[0].id)
    assert refreshed.quantity == 10
    assert refreshed.total == Decimal("25.00")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_meta_returning_default_applies_to_bulk_create_without_explicit_arg(db_generated_field):
    """`Meta.returning = True` on AutoReturningPricedItem means bulk_create() with NO explicit
    `returning=` argument at all still populates pk + GeneratedField, exactly as if the caller
    had passed `returning=True` on every call - the whole point of the Meta default."""
    items = [AutoReturningPricedItem(price=Decimal("2.50"), quantity=3)]
    await AutoReturningPricedItem.objects.bulk_create(items)

    assert items[0].id is not None
    assert items[0].total == Decimal("7.50")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_meta_returning_default_applies_to_bulk_update_without_explicit_arg(db_generated_field):
    """Same Meta.returning fallback, bulk_update() side."""
    created = await AutoReturningPricedItem.objects.create(price=Decimal("2.00"), quantity=2)
    item = await AutoReturningPricedItem.objects.get(id=created.id)

    item.quantity = 5
    await AutoReturningPricedItem.objects.bulk_update([item], fields=["quantity"])

    assert item.total == Decimal("10.00")


@pytest.mark.asyncio
async def test_explicit_returning_false_overrides_meta_returning_true(db_generated_field):
    """An explicit `returning=False` on the call always wins over `Meta.returning = True` -
    dialect-agnostic (this must hold on sqlite too, where returning=True itself would raise)."""
    items = [AutoReturningPricedItem(price=Decimal("1.00"), quantity=1)]
    await AutoReturningPricedItem.objects.bulk_create(items, returning=False)

    assert items[0].id is None
    assert items[0].total is None


@pytest.mark.asyncio
async def test_meta_returning_true_on_sqlite_silently_does_not_populate(db_generated_field):
    """`Meta.returning = True` is a soft preference, not a hard requirement the caller opted
    into - it must never make bulk_create() start raising NotImplementedError on SQLite for
    every model that inherits it. Only an EXPLICIT `returning=True` raises there (see
    test_explicit_returning_true_on_sqlite_still_raises below)."""
    items = [AutoReturningPricedItem(price=Decimal("1.00"), quantity=1)]
    await AutoReturningPricedItem.objects.bulk_create(items)  # must not raise, on any dialect

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    if db_url.split(":", 1)[0].split("+", 1)[0] == "sqlite":
        assert items[0].id is None, "the inherited Meta default must silently no-op on SQLite"


@pytest.mark.asyncio
async def test_meta_returning_true_with_ignore_conflicts_and_no_on_conflict_silently_skips_returning(
    db_generated_field,
):
    """An explicit `returning=True` + `ignore_conflicts=True` without `on_conflict=[...]` is
    rejected up front, but `Meta.returning = True` is only a soft preference - it must never make
    the very common plain `bulk_create(..., ignore_conflicts=True)` start raising on every model
    that inherits it."""
    items = [AutoReturningPricedItem(price=Decimal("1.00"), quantity=1)]
    await AutoReturningPricedItem.objects.bulk_create(items, ignore_conflicts=True)

    assert items[0].id is None, "the inherited Meta default must yield to ignore_conflicts without on_conflict"
    assert await AutoReturningPricedItem.objects.all().count() == 1


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_explicit_returning_true_on_sqlite_still_raises(db_generated_field):
    """Unlike the inherited Meta default, an EXPLICIT `returning=True` on the call must still
    raise on SQLite - the caller asked for something this dialect genuinely can't do."""
    with pytest.raises(UnSupportedError):
        await AutoReturningPricedItem.objects.bulk_create(
            [AutoReturningPricedItem(price=Decimal("1.00"), quantity=1)], returning=True
        )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_meta_returning_true_with_use_copy_silently_skips_returning(db_generated_field_module):
    """`Meta.returning = True` (inherited, not explicit) combined with an explicit
    `use_copy=True` must not raise - COPY simply wins for this call, exactly as if the model had
    no Meta.returning default at all. A project-wide `Meta.returning = True` must never make
    `bulk_create(..., use_copy=True)` start failing on every model that inherits it.

    Uses the module-scoped, non-transaction-wrapped fixture directly - rust_pg's COPY protocol
    has no transaction-participating variant, so this can never run inside the ordinary
    ``db_generated_field``'s rollback-wrapped transaction; the inserted row is deleted by hand
    at the end instead, to keep the module-scoped connection's state clean for later tests.
    """
    items = [AutoReturningPricedItem(price=Decimal("1.00"), quantity=1)]
    await AutoReturningPricedItem.objects.bulk_create(items, use_copy=True)

    assert items[0].id is None, "use_copy must silently suppress the inherited Meta.returning default"

    await AutoReturningPricedItem.objects.filter(price=Decimal("1.00"), quantity=1).delete()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_explicit_returning_true_with_use_copy_still_raises(db_generated_field):
    """Unlike the inherited Meta default, an EXPLICIT `returning=True` combined with
    `use_copy=True` must still raise - directly contradictory requests, not a soft preference
    yielding to a more specific one."""
    with pytest.raises(UnSupportedError, match="mutually exclusive"):
        await AutoReturningPricedItem.objects.bulk_create(
            [AutoReturningPricedItem(price=Decimal("1.00"), quantity=1)], use_copy=True, returning=True
        )


@pytest.mark.asyncio
async def test_fk_shadow_column_targeting_a_generated_field_is_inserted(db_generated_field):
    """A FK's own shadow column is built via `copy(related_field)` when `to_field=` points at a
    GeneratedField, to inherit its type/constraints - but that copy's own `.generated` is reset
    to the FK field's own (ordinarily False) `generated` kwarg, since the shadow column itself is
    an ordinary, writable column, not itself DB-generated. `_prepare_insert_columns()` used to
    skip it from every INSERT purely because it was still an instance of the GeneratedField
    class, regardless of that reset `.generated` flag - the shadow column silently stayed NULL
    forever instead of ever storing the FK value, via .create(), .save(), and bulk_create() alike."""
    warehouse = await GeneratedFieldFkTargetWarehouse.objects.create(prefix="WH", number=7)
    assert warehouse.code == "WH-7"

    created = await GeneratedFieldFkTargetStockItem.objects.create(warehouse=warehouse, name="widget")
    assert created.warehouse_id == "WH-7"
    fetched = await GeneratedFieldFkTargetStockItem.objects.get(id=created.id)
    assert fetched.warehouse_id == "WH-7"

    saved = GeneratedFieldFkTargetStockItem(warehouse=warehouse, name="gadget")
    await saved.save()
    assert (await GeneratedFieldFkTargetStockItem.objects.get(id=saved.id)).warehouse_id == "WH-7"

    bulk_items = [GeneratedFieldFkTargetStockItem(warehouse=warehouse, name="gizmo")]
    await GeneratedFieldFkTargetStockItem.objects.bulk_create(bulk_items)
    stored = await GeneratedFieldFkTargetStockItem.objects.get(name="gizmo")
    assert stored.warehouse_id == "WH-7"


@pytest.mark.asyncio
async def test_date_part_lookup_on_a_generated_datetime_field_is_timezone_aware(db_generated_field):
    """get_filters_for_field()'s `__year`/`__month`/`__hour`/etc date-part lookups read
    `isinstance(field, DatetimeField)` (not the GeneratedField-unwrapped `effective_field`
    every OTHER branch in that function already uses) to decide whether to apply the
    configured timezone before extracting - False for a GeneratedField(output_field=
    DatetimeField()) column, so the lookup silently extracted in server-naive time instead of
    the configured zone. Confirmed live before this fix: identical `created_at`/`logged_at`
    columns (`logged_at` just mirrors `created_at`) gave DIFFERENT `__hour` results under a
    non-UTC zone."""
    with override_timezone(use_tz=True, timezone="America/New_York"):
        from datetime import UTC, datetime

        # 10:30 UTC is 06:30 in America/New_York (EDT, UTC-4) - the configured zone must be
        # applied before extracting `hour`, or this reads back as 10, not 6.
        obj = await TimestampedItem.objects.create(created_at=datetime(2024, 6, 15, 10, 30, tzinfo=UTC))
        await obj.refresh_from_db()

        assert await TimestampedItem.objects.filter(id=obj.id, created_at__hour=6).count() == 1
        assert await TimestampedItem.objects.filter(id=obj.id, logged_at__hour=6).count() == 1


@pytest.mark.asyncio
async def test_avg_of_a_generated_int_field_keeps_fractional_part(db_generated_field):
    """Avg._coerce_output_field() read `isinstance(field_object, IntField)` directly - False for
    a GeneratedField(output_field=IntField()) column even though it IS one at the SQL level, so
    the FloatField coercion never kicked in and the average came back as a truncated int instead
    of a float. Confirmed live before this fix: quantity_doubled values 2, 2, 2, 4 (real average
    2.5) came back as 2."""
    await AveragedQuantityItem.objects.create(quantity=1)
    await AveragedQuantityItem.objects.create(quantity=1)
    await AveragedQuantityItem.objects.create(quantity=1)
    await AveragedQuantityItem.objects.create(quantity=2)

    result = await AveragedQuantityItem.objects.all().aggregate(avg=Avg("quantity_doubled"))

    assert result == {"avg": pytest.approx(2.5)}
    assert isinstance(result["avg"], float)
