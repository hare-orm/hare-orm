"""The columnar test dialect - a dialect hare doesn't ship, plugged in through its public API - and
how hare's core reacts to a database without transactions, foreign keys or unique constraints."""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test.helpers import hare_test_context
from hare.dialects.registry import DialectRegistry
from hare.exceptions import ConfigurationError, ProtectedError, UnSupportedError
from hare.migrations.drift import detect_drift
from hare.query.functions import Length
from hare.transactions.transactions import Transactions
from tests.dialects.columnar.dialect import COLUMNAR_DIALECT
from tests.dialects.columnar.driver import COLUMNAR_DRIVER, ColumnarClient
from tests.dialects.columnar.models import Keeper, Pinned, Reading, Shelf, Volume
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model

MODELS_MODULE = "tests.dialects.columnar.models"


@pytest_asyncio.fixture
async def columnar() -> AsyncGenerator[Any]:
    async with hare_test_context([MODELS_MODULE], db_url="columnar://:memory:") as context:
        yield context


def test_the_driver_and_dialect_are_registered():
    assert DialectRegistry.get_driver("columnar") is COLUMNAR_DRIVER
    assert DialectRegistry.get_driver_for_url_scheme("columnar") is COLUMNAR_DRIVER
    assert DialectRegistry.get_dialect("columnar") is COLUMNAR_DIALECT
    assert COLUMNAR_DRIVER in DialectRegistry.get_drivers()


@pytest.mark.asyncio
async def test_a_columnar_url_connects_through_the_driver(columnar):
    connection = columnar.db()
    assert isinstance(connection, ColumnarClient)
    assert connection.dialect is COLUMNAR_DIALECT
    assert not connection.features.supports_transactions


@pytest.mark.asyncio
async def test_queries_quote_with_backticks_and_number_their_placeholders(columnar):
    first = await Shelf.objects.create(name="first")
    second = await Shelf.objects.create(name="second")
    sql = Shelf.objects.filter(name="first", room="").sql()
    assert sql == "SELECT `id`,`name`,`room` FROM `shelf` WHERE `name`=?1 AND `room`=?2"
    # The same query shape again, with other values bound into the cached SQL.
    assert [shelf.id for shelf in await Shelf.objects.filter(name="first", room="")] == [first.id]
    assert [shelf.id for shelf in await Shelf.objects.filter(name="second", room="")] == [second.id]


@pytest.mark.asyncio
async def test_tables_get_no_foreign_key_or_unique_constraint(columnar):
    schema_sql = columnar.db().get_schema_sql(safe=False)
    assert "REFERENCES" not in schema_sql
    assert "FOREIGN KEY" not in schema_sql
    assert "UNIQUE" not in schema_sql
    assert "CREATE TABLE `volume`" in schema_sql
    # The room index keeps its unique index's name, as a plain index.
    assert "CREATE INDEX `uidx_shelf_room_" in schema_sql
    await Shelf.objects.create(name="twin")
    await Shelf.objects.create(name="twin")
    assert await Shelf.objects.filter(name="twin").count() == 2


@pytest.mark.asyncio
async def test_on_delete_runs_in_python(columnar):
    shelf = await Shelf.objects.create(name="history")
    await Volume.objects.create(title="Rome", shelf=shelf)
    await Volume.objects.create(title="Greece", shelf=shelf)
    guarded = await Shelf.objects.create(name="guarded")
    await Keeper.objects.create(name="ann", shelf=guarded)

    await shelf.delete()
    assert await Volume.objects.all().count() == 0
    with pytest.raises(ProtectedError):
        await guarded.delete()
    with pytest.raises(ProtectedError):
        await Shelf.objects.filter(name="guarded").delete()
    assert await Shelf.objects.filter(name="guarded").exists()


@pytest.mark.asyncio
async def test_transactions_are_refused(columnar):
    with pytest.raises(UnSupportedError, match="columnar database, which has no transactions"):
        async with Transactions.atomic():
            pass

    @Transactions.atomic()
    async def write() -> None:
        await Shelf.objects.create(name="never")

    with pytest.raises(UnSupportedError, match="has no transactions"):
        await write()
    assert not await Shelf.objects.filter(name="never").exists()


@pytest.mark.asyncio
async def test_a_multi_statement_write_runs_statement_by_statement(columnar):
    await Shelf.objects.bulk_create([Shelf(name=f"s{number}") for number in range(5)], batch_size=2)
    assert await Shelf.objects.all().count() == 5
    shelf = await Shelf.objects.first()
    volume = await Volume.objects.create(title="Moby", shelf=shelf)
    volume.title = "Moby Dick"
    await volume.save()
    assert (await Volume.objects.get(id=volume.id)).title == "Moby Dick"


@pytest.mark.asyncio
async def test_an_upsert_can_target_the_primary_key_only(columnar):
    shelf = await Shelf.objects.create(name="upsert")
    await Shelf.objects.bulk_create([Shelf(id=shelf.id, name="upserted")], update_fields=["name"], on_conflict=["id"])
    assert (await Shelf.objects.get(id=shelf.id)).name == "upserted"
    with pytest.raises(UnSupportedError, match="only the primary key can conflict"):
        await Shelf.objects.bulk_create([Shelf(name="upsert")], update_fields=["room"], on_conflict=["name"])
    with pytest.raises(UnSupportedError, match="on_conflict_constraint is not supported by the columnar dialect"):
        await Shelf.objects.bulk_create(
            [Shelf(name="upsert")], update_fields=["room"], on_conflict_constraint="shelf_room_name"
        )


@pytest.mark.asyncio
async def test_a_uuid_is_stored_as_sixteen_bytes(columnar):
    shelf = await Shelf.objects.create(name="uuid")
    code = uuid.uuid4()
    volume = await Volume.objects.create(title="Dune", shelf=shelf, code=code)
    rows = await columnar.db().execute_dicts(
        "SELECT typeof(`code`) AS value_type, length(`code`) AS size FROM `volume`"
    )
    assert rows == [{"value_type": "blob", "size": 16}]
    assert (await Volume.objects.get(code=code)).id == volume.id
    assert (await Volume.objects.get(id=volume.id)).code == code
    assert await Volume.objects.filter(code__in=[code, uuid.uuid4()]).count() == 1
    assert await Volume.objects.all().values_list("code", flat=True) == [code]


@pytest.mark.asyncio
async def test_a_function_renders_the_dialects_way(columnar):
    await Shelf.objects.create(name="abc")
    queryset = Shelf.objects.annotate(name_length=Length("name")).filter(name="abc")
    assert "length(CAST(`name` AS TEXT))" in queryset.sql()
    assert (await queryset.first()).name_length == 3


@pytest.mark.asyncio
async def test_the_dialect_adds_a_queryset_method(columnar):
    await Shelf.objects.bulk_create([Shelf(name=f"sample{number}") for number in range(10)])
    assert await Shelf.objects.all().sample(100).count() == 10
    assert await Shelf.objects.all().sample(0).count() == 0
    assert "abs(random()) % 100 < 50" in Shelf.objects.all().sample(50).sql()
    with pytest.raises(ValueError, match="percent from 0 to 100"):
        await Shelf.objects.all().sample(101)


@pytest.mark.asyncio
async def test_a_model_without_a_primary_key_in_a_strict_table(columnar):
    schema_sql = columnar.db().get_schema_sql(safe=False)
    reading_sql = next(statement for statement in schema_sql.split(";") if "`reading`" in statement)
    assert reading_sql.rstrip().endswith(") STRICT")
    assert "WITHOUT ROWID" not in reading_sql
    await Reading.objects.bulk_create([Reading(title="Dune", pages=412), Reading(title="Emma", pages=474)])
    await Reading.objects.create(title="Ulysses", pages=730)
    assert await Reading.objects.filter(pages__gt=450).count() == 2
    assert await Reading.objects.filter(title="Dune").update(pages=413) == 1
    assert await Reading.objects.filter(title="Dune").values_list("pages", flat=True) == [413]
    with pytest.raises(ConfigurationError, match="has no primary key"):
        await (await Reading.objects.first()).delete()


@pytest.mark.asyncio
async def test_table_options_of_the_dialect(columnar):
    schema_sql = columnar.db().get_schema_sql(safe=False)
    pinned_sql = next(statement for statement in schema_sql.split(";") if "`pinned`" in statement)
    assert pinned_sql.rstrip().endswith(") WITHOUT ROWID")
    await Pinned.objects.create(id=7, note="seven")
    assert (await Pinned.objects.get(id=7)).note == "seven"


@pytest.mark.asyncio
async def test_migrations_leave_no_drift_and_no_pending_change(columnar):
    round_trip = RoundTrip(columnar.db())
    from hare import fields
    from hare.ddl.constraints import UniqueConstraint
    from hare.ddl.indexes import Index

    def author_fields(unique: bool) -> dict[str, Any]:
        return {"name": fields.CharField(max_length=40, unique=unique), "city": fields.CharField(max_length=40)}

    author = build_model(
        "Author",
        "migrated_author",
        author_fields(unique=True),
        {
            "constraints": [
                UniqueConstraint(fields=("name", "city")),
                UniqueConstraint(fields=("city", "name"), name="author_city_name"),
            ],
            "indexes": [Index(fields=("city",), unique=True)],
        },
    )
    await round_trip.migrate_to(author)
    assert round_trip.get_pending_operations(author) == []
    drift = await detect_drift(round_trip.connection, build_live_state(author), [APP_LABEL])
    assert [operation.describe() for operation in drift.operations] == []

    plain_author = build_model("Author", "migrated_author", author_fields(unique=False))
    await round_trip.migrate_to(plain_author)
    assert round_trip.get_pending_operations(plain_author) == []
    assert (await detect_drift(round_trip.connection, build_live_state(plain_author), [APP_LABEL])).operations == []
    await round_trip.migrate_to(author)
    assert (await detect_drift(round_trip.connection, build_live_state(author), [APP_LABEL])).operations == []


@pytest.mark.asyncio
async def test_a_json_object_holds_a_uuid_stored_as_bytes_as_its_text(columnar):
    from hare.query.functions import JSONObject

    shelf = await Shelf.objects.create(name="json")
    code = uuid.uuid4()
    await Volume.objects.create(title="Dune", shelf=shelf, code=code)
    written = await Volume.objects.annotate(written=JSONObject(code="code")).values_list("written", flat=True)
    assert written == [{"code": str(code)}]
    assert await Volume.objects.annotate(written=JSONObject(code="code")).filter(written__code=str(code)).count() == 1


@pytest.mark.asyncio
async def test_drift_and_inspectdb_read_the_dialects_table_options(columnar):
    from hare import fields
    from hare.inspectdb import SchemaInspector
    from tests.dialects.columnar.table_options import ColumnarTableOptions

    round_trip = RoundTrip(columnar.db())

    def build_counter(table_options: list[ColumnarTableOptions]) -> type:
        return build_model(
            "Counter",
            "strict_counter",
            {"id": fields.IntField(primary_key=True, generated=False), "hits": fields.IntField()},
            {"table_options": table_options},
        )

    strict_counter = build_counter([ColumnarTableOptions(strict=True, without_rowid=True)])
    await round_trip.migrate_to(strict_counter)
    assert (await detect_drift(round_trip.connection, build_live_state(strict_counter), [APP_LABEL])).operations == []
    loose_drift = await detect_drift(round_trip.connection, build_live_state(build_counter([])), [APP_LABEL])
    assert [type(operation).__name__ for operation in loose_drift.operations] == ["AlterModelOptions"]
    source = await SchemaInspector.inspect(round_trip.connection, ["strict_counter"])
    assert "table_options = [ColumnarTableOptions(strict=True, without_rowid=True)]" in source
