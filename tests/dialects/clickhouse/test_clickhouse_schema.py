"""ClickHouse tables: created with their engine and sort, read back by the introspector, changed by
the schema editor - a column added, renamed, retyped and dropped, an index added."""

import pytest

from hare.ddl.indexes.index import Index
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.data.numeric import BigIntField, IntField
from hare.fields.data.text import CharField, TextField
from hare.fields.enums import OnDelete
from hare.fields.relations.fields import ForeignKeyField
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.inspected_field_specification import InspectedFieldSpecification
from hare.migrations.operations import (
    AddField,
    AddIndex,
    AlterField,
    BackfillColumn,
    CreateModel,
    DeleteModel,
    RemoveField,
    RemoveIndex,
    RenameField,
)
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from tests.dialects.clickhouse.models import PageView, Player, Team


@pytest.mark.asyncio
async def test_tables_are_created_with_their_engine_and_sort(clickhouse_db):
    connection = Player._meta.connection
    rows = await connection.execute_dicts(
        "SELECT name, engine, sorting_key, partition_key, comment FROM system.tables "
        "WHERE database = currentDatabase() ORDER BY name"
    )
    tables = {row["name"]: row for row in rows}
    assert tables["player"]["engine"] == "MergeTree"
    assert tables["player"]["sorting_key"] == "id"
    assert tables["pageview"]["sorting_key"] == "id, site, viewed_at"
    assert tables["pageview"]["partition_key"] == "toYYYYMM(viewed_at)"
    assert tables["team"]["comment"] == "Teams of players"
    assert tables["player_skill"]["sorting_key"] == ""


@pytest.mark.asyncio
async def test_the_introspector_reads_the_tables(clickhouse_db):
    connection = Player._meta.connection
    assert {"player", "team", "skill", "pageview"} <= set(await DatabaseCatalog.get_table_names(connection))
    assert await DatabaseCatalog.table_exists(connection, "player")
    assert not await DatabaseCatalog.table_exists(connection, "no_such_table")
    (player,) = await DatabaseCatalog.inspect_tables(connection, ["player"])
    columns = {column.name: column for column in player.columns}
    assert columns["id"].is_pk
    assert not columns["id"].nullable
    assert columns["team_id"].nullable
    assert (columns["score"].numeric_precision, columns["score"].numeric_scale) == (10, 2)
    assert columns["joined"].db_type == "DateTime64(6, 'UTC')"
    (team,) = await DatabaseCatalog.inspect_tables(connection, ["team"])
    assert team.table_description == "Teams of players"
    assert {column.name: column.description for column in team.columns}["description"] == "What the team does"


async def get_columns(connection, table):
    (table_info,) = await DatabaseCatalog.inspect_tables(connection, [table])
    return {column.name: column for column in table_info.columns}


@pytest.mark.asyncio
async def test_migrations_create_change_and_drop_a_table(clickhouse_db):
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    operations = [
        CreateModel(
            name="Article",
            fields=[("id", BigIntField(primary_key=True, generated=False)), ("title", CharField(max_length=200))],
            options={"table": "clickhouse_article"},
        ),
        AddField(model_name="Article", name="subtitle", field=CharField(max_length=200, null=True)),
    ]
    for operation in operations:
        await operation.run("models", state, dry_run=False, state_editor=editor)
    await connection.execute_script(
        'INSERT INTO "clickhouse_article" ("id", "title", "subtitle") VALUES (1, \'a\', \'b\')'
    )
    columns = await get_columns(connection, "clickhouse_article")
    assert columns["subtitle"].nullable
    await RenameField(model_name="Article", old_name="subtitle", new_name="lede").run(
        "models", state, dry_run=False, state_editor=editor
    )
    await AlterField(model_name="Article", name="title", field=TextField()).run(
        "models", state, dry_run=False, state_editor=editor
    )
    assert await connection.execute_dicts('SELECT "title", "lede" FROM "clickhouse_article"') == [
        {"title": "a", "lede": "b"}
    ]
    await AlterField(model_name="Article", name="title", field=TextField(null=True)).run(
        "models", state, dry_run=False, state_editor=editor
    )
    assert (await get_columns(connection, "clickhouse_article"))["title"].nullable
    await AlterField(model_name="Article", name="title", field=TextField()).run(
        "models", state, dry_run=False, state_editor=editor
    )
    assert not (await get_columns(connection, "clickhouse_article"))["title"].nullable
    await AddField(model_name="Article", name="views", field=IntField(default=7)).run(
        "models", state, dry_run=False, state_editor=editor
    )
    assert await connection.execute_dicts('SELECT "views" FROM "clickhouse_article"') == [{"views": 7}]
    await AddIndex(model_name="Article", index=Index(fields=("views",), name="clickhouse_article_views")).run(
        "models", state, dry_run=False, state_editor=editor
    )
    (article,) = await DatabaseCatalog.inspect_tables(connection, ["clickhouse_article"])
    # A plain one-column index is a field flag, as on the other databases.
    assert [index.name for index in article.column_indexes] == ["clickhouse_article_views"]
    assert [column.name for column in article.columns if column.has_index] == ["views"]
    await RemoveIndex(model_name="Article", name="clickhouse_article_views").run(
        "models", state, dry_run=False, state_editor=editor
    )
    (article,) = await DatabaseCatalog.inspect_tables(connection, ["clickhouse_article"])
    assert article.indexes == article.column_indexes == []
    await RemoveField(model_name="Article", name="lede").run("models", state, dry_run=False, state_editor=editor)
    await RemoveField(model_name="Article", name="views").run("models", state, dry_run=False, state_editor=editor)
    assert set(await get_columns(connection, "clickhouse_article")) == {"id", "title"}
    await DeleteModel(name="Article").run("models", state, dry_run=False, state_editor=editor)
    assert not await DatabaseCatalog.table_exists(connection, "clickhouse_article")


@pytest.mark.asyncio
async def test_an_added_column_takes_its_default_before_its_comment(clickhouse_db):
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    operations = [
        CreateModel(
            name="Counter",
            fields=[("id", BigIntField(primary_key=True, generated=False))],
            options={"table": "clickhouse_counter"},
        ),
        AddField(model_name="Counter", name="count", field=IntField(db_default=3, description="how many")),
    ]
    for operation in operations:
        await operation.run("models", state, dry_run=False, state_editor=editor)
    await connection.execute_script('INSERT INTO "clickhouse_counter" ("id") VALUES (1)')
    assert await connection.execute_dicts('SELECT "count" FROM "clickhouse_counter"') == [{"count": 3}]
    assert (await get_columns(connection, "clickhouse_counter"))["count"].description == "how many"
    await DeleteModel(name="Counter").run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_rows_stored_before_a_column_was_added_keep_its_default_through_a_change_of_it(clickhouse_db):
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Tally",
        fields=[("id", BigIntField(primary_key=True, generated=False))],
        options={"table": "clickhouse_tally"},
    ).run("models", state, dry_run=False, state_editor=editor)
    await connection.execute_script('INSERT INTO "clickhouse_tally" ("id") VALUES (1)')
    for operation in (
        AddField(model_name="Tally", name="count", field=IntField(db_default=3)),
        AlterField(model_name="Tally", name="count", field=IntField(db_default=9)),
    ):
        await operation.run("models", state, dry_run=False, state_editor=editor)
    await connection.execute_script('INSERT INTO "clickhouse_tally" ("id") VALUES (2)')
    # The stored row got the default of the time it was added, as PostgreSQL writes it.
    assert await connection.execute_dicts('SELECT "id", "count" FROM "clickhouse_tally" ORDER BY "id"') == [
        {"id": 1, "count": 3},
        {"id": 2, "count": 9},
    ]
    await DeleteModel(name="Tally").run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_an_indexed_column_changes_its_type_and_nullability(clickhouse_db):
    """ClickHouse refuses a MODIFY COLUMN of a column a data skipping index covers - the indexes are
    dropped around the change and created again."""
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    operations = [
        CreateModel(
            name="Sheet",
            fields=[
                ("id", BigIntField(primary_key=True, generated=False)),
                ("code", IntField(null=True, db_index=True)),
                ("label", CharField(max_length=20, default="x")),
            ],
            options={"table": "clickhouse_sheet", "indexes": (Index(fields=("code", "label")),)},
        ),
        AlterField(model_name="Sheet", name="code", field=IntField(default=0, db_index=True)),
        AlterField(model_name="Sheet", name="label", field=CharField(max_length=60, default="x")),
        AlterField(model_name="Sheet", name="code", field=BigIntField(default=0)),
    ]
    for operation in operations:
        await operation.run("models", state, dry_run=False, state_editor=editor)
    (table,) = await DatabaseCatalog.inspect_tables(connection, ["clickhouse_sheet"])
    assert not table.column_indexes
    assert [(index.columns, index.index_type) for index in table.indexes] == [(["code", "label"], "")]
    assert not next(column for column in table.columns if column.name == "code").nullable


@pytest.mark.asyncio
async def test_an_index_is_a_data_skipping_index(clickhouse_db):
    connection = PageView._meta.connection
    await connection.execute_script(
        'CREATE INDEX "pageview_duration_idx" ON "pageview" ("duration_ms") TYPE minmax GRANULARITY 1'
    )
    await connection.execute_script(
        'CREATE INDEX "pageview_site_idx" ON "pageview" ("site") TYPE bloom_filter GRANULARITY 1'
    )
    (table,) = await DatabaseCatalog.inspect_tables(connection, ["pageview"])
    assert [index.name for index in table.column_indexes] == ["pageview_duration_idx"]
    assert [(index.name, index.index_type) for index in table.indexes] == [("pageview_site_idx", "bloom_filter")]


@pytest.mark.asyncio
async def test_the_introspector_maps_a_type_by_its_whole_name(clickhouse_db):
    connection = Player._meta.connection
    await connection.execute_script(
        "CREATE TABLE typed (id UInt64, small UInt8, medium UInt16, large UInt32, numbers Array(Int64), "
        "pairs Map(String, String), amount Decimal(12, 3), moment DateTime64(6, 'UTC'), day Date32, "
        "code FixedString(4), ratio Float32) ENGINE = MergeTree ORDER BY id"
    )
    columns = await get_columns(connection, "typed")
    introspector = DatabaseCatalog.get_introspector_class(connection)
    mapped = {name: introspector.map_column_type(column) for name, column in columns.items()}

    assert mapped == {
        "id": ("hare.dialects.clickhouse.fields.UInt64Field", {}, False),
        "small": ("hare.dialects.clickhouse.fields.UInt8Field", {}, False),
        "medium": ("hare.dialects.clickhouse.fields.UInt16Field", {}, False),
        "large": ("hare.dialects.clickhouse.fields.UInt32Field", {}, False),
        "numbers": (
            "hare.fields.data.containers.ArrayField",
            {"base_field": InspectedFieldSpecification("hare.fields.data.numeric.BigIntField", {})},
            False,
        ),
        "pairs": (
            "hare.fields.data.containers.MapField",
            {
                "key_field": InspectedFieldSpecification("hare.fields.data.text.TextField", {}),
                "value_field": InspectedFieldSpecification("hare.fields.data.text.TextField", {}),
            },
            False,
        ),
        "amount": ("hare.fields.data.numeric.DecimalField", {"max_digits": 12, "decimal_places": 3}, False),
        "moment": ("hare.fields.data.temporal.DatetimeField", {}, False),
        "day": ("hare.fields.data.temporal.DateField", {}, False),
        "code": ("hare.dialects.clickhouse.fields.FixedStringField", {"length": 4}, False),
        "ratio": ("hare.dialects.clickhouse.fields.Float32Field", {}, False),
    }


@pytest.mark.asyncio
async def test_the_introspector_maps_an_enum_onto_the_enum_field_writing_it(clickhouse_db):
    """An enum column was read back as a CharField of its labels - not the enum the column holds."""
    from hare.dialects.clickhouse.types.clickhouse_enum_types import ClickhouseEnumTypes

    connection = Player._meta.connection
    color_type = ClickhouseEnumTypes.get_enum_type(ClickhouseEnumTypes.get_label_numbers(["red", "dark blue"]))
    await connection.execute_script(
        f"CREATE TABLE enums (id UInt64, color {color_type}, level Enum8('LOW' = 1, 'HIGH' = 2), "
        "mood Enum8('so-so' = 1), wide Enum16('LOW' = 1)) ENGINE = MergeTree ORDER BY id"
    )
    columns = await get_columns(connection, "enums")
    introspector = DatabaseCatalog.get_introspector_class(connection)
    mapped = {name: introspector.map_column_type(column) for name, column in columns.items() if name != "id"}

    color_path, color_kwargs, color_ambiguous = mapped["color"]
    assert (color_path, color_ambiguous) == ("hare.fields.data.choices.CharEnumField", False)
    color_enum = color_kwargs["enum_type"]
    assert color_enum.__name__ == "ColorLabels"
    assert {member.name: member.value for member in color_enum} == {"RED": "red", "DARK_BLUE": "dark blue"}
    level_path, level_kwargs, level_ambiguous = mapped["level"]
    assert (level_path, level_ambiguous) == ("hare.fields.data.choices.IntEnumField", False)
    assert {member.name: member.value for member in level_kwargs["enum_type"]} == {"LOW": 1, "HIGH": 2}
    # Labels no member is named by, and an Enum16 the field would write as an Enum8.
    assert mapped["mood"] == ("hare.fields.data.text.CharField", {"max_length": 5}, True)
    assert mapped["wide"] == ("hare.fields.data.text.CharField", {"max_length": 3}, True)


@pytest.mark.asyncio
async def test_a_narrowing_that_would_lose_data_is_refused(clickhouse_db):
    from hare.fields import ArrayField, DecimalField, MapField, SmallIntField, TupleField
    from hare.migrations.exceptions import FieldNarrowingDataLossError

    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Narrow",
        fields=[
            ("id", BigIntField(primary_key=True, generated=False)),
            ("count", IntField()),
            ("price", DecimalField(max_digits=10, decimal_places=2)),
            ("counts", ArrayField(ArrayField(IntField()))),
            ("by_name", MapField(CharField(max_length=10), TupleField([IntField(), CharField(max_length=10)]))),
        ],
        options={"table": "clickhouse_narrow"},
    ).run("models", state, dry_run=False, state_editor=editor)
    await connection.execute_script(
        'INSERT INTO "clickhouse_narrow" VALUES '
        "(1, 100000, 123.45, [[1, 100000]], map('a', tuple(100000, 'abcdefghij')))"
    )
    for name, narrowed_field in (
        ("count", SmallIntField()),
        ("price", DecimalField(max_digits=5, decimal_places=1)),
        ("counts", ArrayField(ArrayField(SmallIntField()))),
        ("by_name", MapField(CharField(max_length=10), TupleField([IntField(), CharField(max_length=5)]))),
        ("by_name", MapField(CharField(max_length=10), TupleField([SmallIntField(), CharField(max_length=10)]))),
    ):
        with pytest.raises(FieldNarrowingDataLossError):
            await AlterField(model_name="Narrow", name=name, field=narrowed_field).run(
                "models", state, dry_run=False, state_editor=editor
            )
    await AlterField(model_name="Narrow", name="counts", field=ArrayField(ArrayField(BigIntField()))).run(
        "models", state, dry_run=False, state_editor=editor
    )
    assert await connection.execute_dicts('SELECT "count", "counts" FROM "clickhouse_narrow"') == [
        {"count": 100000, "counts": [[1, 100000]]}
    ]
    await DeleteModel(name="Narrow").run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_a_decimal_column_turned_into_an_integer_is_checked_against_its_range(clickhouse_db):
    """The check compared Decimal(12, 2) with the Int64 bounds, which raised DECIMAL_OVERFLOW."""
    from hare.fields import DecimalField
    from hare.migrations.exceptions import FieldNarrowingDataLossError

    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Amounts",
        fields=[
            ("id", BigIntField(primary_key=True, generated=False)),
            ("whole", DecimalField(max_digits=12, decimal_places=2)),
            ("split", DecimalField(max_digits=12, decimal_places=2)),
        ],
        options={"table": "clickhouse_amounts"},
    ).run("models", state, dry_run=False, state_editor=editor)
    await connection.execute_script('INSERT INTO "clickhouse_amounts" VALUES (1, 5, 6.5)')
    with pytest.raises(FieldNarrowingDataLossError):
        await AlterField(model_name="Amounts", name="split", field=BigIntField()).run(
            "models", state, dry_run=False, state_editor=editor
        )
    await AlterField(model_name="Amounts", name="whole", field=BigIntField()).run(
        "models", state, dry_run=False, state_editor=editor
    )
    await AlterField(model_name="Amounts", name="whole", field=IntField()).run(
        "models", state, dry_run=False, state_editor=editor
    )
    assert await connection.execute_dicts('SELECT "whole" FROM "clickhouse_amounts"') == [{"whole": 5}]
    await DeleteModel(name="Amounts").run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_a_column_made_nullable_reads_its_values_as_not_null(clickhouse_db):
    """ClickHouse 25.8 read the NULL map the stored parts lacked as every value NULL: a filter by
    IS NULL matched every row stored before the change."""
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Titled",
        fields=[("id", BigIntField(primary_key=True, generated=False)), ("title", CharField(max_length=20))],
        options={"table": "clickhouse_titled"},
    ).run("models", state, dry_run=False, state_editor=editor)
    await connection.execute_script("INSERT INTO \"clickhouse_titled\" VALUES (1, 'a')")
    await AlterField(model_name="Titled", name="title", field=CharField(max_length=20, null=True)).run(
        "models", state, dry_run=False, state_editor=editor
    )
    assert await connection.execute_dicts(
        'SELECT count(*) AS null_count FROM "clickhouse_titled" WHERE "title" IS NULL'
    ) == [{"null_count": 0}]
    await DeleteModel(name="Titled").run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_a_column_holding_null_is_not_made_not_null(clickhouse_db):
    """The MODIFY COLUMN failed in its mutation, which stayed and failed every later one - a fix of
    the data by an UPDATE too."""
    from hare.exceptions import IntegrityError

    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Titled",
        fields=[
            ("id", BigIntField(primary_key=True, generated=False)),
            ("title", CharField(max_length=20, null=True)),
        ],
        options={"table": "clickhouse_titled"},
    ).run("models", state, dry_run=False, state_editor=editor)
    await connection.execute_script('INSERT INTO "clickhouse_titled" VALUES (1, NULL)')
    with pytest.raises(IntegrityError, match="contains null values"):
        await AlterField(model_name="Titled", name="title", field=CharField(max_length=20)).run(
            "models", state, dry_run=False, state_editor=editor
        )
    await connection.execute_script('ALTER TABLE "clickhouse_titled" UPDATE "title" = \'a\' WHERE 1')
    await AlterField(model_name="Titled", name="title", field=CharField(max_length=20)).run(
        "models", state, dry_run=False, state_editor=editor
    )
    assert await connection.execute_dicts('SELECT "title" FROM "clickhouse_titled"') == [{"title": "a"}]
    await DeleteModel(name="Titled").run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_an_enum_column_follows_its_members(clickhouse_db):
    from enum import IntEnum, StrEnum

    from hare.fields import CharEnumField, IntEnumField
    from hare.migrations.exceptions import FieldNarrowingDataLossError

    class Color(StrEnum):
        RED = "red"
        BLUE = "blue"

    class WiderColor(StrEnum):
        RED = "red"
        BLUE = "blue"
        GREEN = "green"

    class NarrowerColor(StrEnum):
        RED = "red"

    class Level(IntEnum):
        LOW = 1
        HIGH = 2

    class NarrowerLevel(IntEnum):
        LOW = 1

    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Painted",
        fields=[
            ("id", BigIntField(primary_key=True, generated=False)),
            ("color", CharEnumField(Color)),
            ("level", IntEnumField(Level)),
        ],
        options={"table": "clickhouse_painted"},
    ).run("models", state, dry_run=False, state_editor=editor)
    await connection.execute_script("INSERT INTO \"clickhouse_painted\" VALUES (1, 'red', 'LOW'), (2, 'blue', 'HIGH')")
    # A member the rows hold can't be left out - the column would keep a number of no label.
    for name, narrowed_field in (("color", CharEnumField(NarrowerColor)), ("level", IntEnumField(NarrowerLevel))):
        with pytest.raises(FieldNarrowingDataLossError):
            await AlterField(model_name="Painted", name=name, field=narrowed_field).run(
                "models", state, dry_run=False, state_editor=editor
            )
    await AlterField(model_name="Painted", name="color", field=CharEnumField(WiderColor)).run(
        "models", state, dry_run=False, state_editor=editor
    )
    await connection.execute_script("INSERT INTO \"clickhouse_painted\" VALUES (3, 'green', 'LOW')")
    rows = await connection.execute_dicts('SELECT "id", "color", "level" FROM "clickhouse_painted" ORDER BY "id"')
    assert [(row["id"], row["color"], row["level"]) for row in rows] == [
        (1, "red", "LOW"),
        (2, "blue", "HIGH"),
        (3, "green", "LOW"),
    ]
    await connection.execute_script(
        'ALTER TABLE "clickhouse_painted" DELETE WHERE "id" = 2 SETTINGS mutations_sync = 2'
    )
    await AlterField(model_name="Painted", name="level", field=IntEnumField(NarrowerLevel)).run(
        "models", state, dry_run=False, state_editor=editor
    )
    await DeleteModel(name="Painted").run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_a_backfill_fills_every_row(clickhouse_db):
    """A mutation reports no count of rows: the batches ended after the first one, and the rows
    beyond it kept their NULL."""
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    operations = [
        CreateModel(
            name="Parcel",
            fields=[
                ("id", BigIntField(primary_key=True, generated=False)),
                ("code", CharField(max_length=10, null=True)),
            ],
            options={"table": "clickhouse_parcel"},
        ),
    ]
    for operation in operations:
        await operation.run("models", state, dry_run=False, state_editor=editor)
    await connection.execute_script(
        'INSERT INTO "clickhouse_parcel" ("id", "code") SELECT number, if(number = 3, \'kept\', NULL) FROM numbers(7)'
    )

    await BackfillColumn(model_name="Parcel", field_name="code", value="x", batch_size=2).run(
        "models", state, dry_run=False, state_editor=editor
    )

    assert await connection.execute_dicts(
        'SELECT "code", count() AS "rows" FROM "clickhouse_parcel" GROUP BY "code" ORDER BY "code"'
    ) == [{"code": "kept", "rows": 1}, {"code": "x", "rows": 6}]
    await DeleteModel(name="Parcel").run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True])
async def test_a_relation_made_nullable_keeps_the_index_of_its_key_column(clickhouse_db, composite):
    """A data skipping index blocks a MODIFY COLUMN of the column it covers - the key column's index,
    the one over every key column of a relation to a composite key, is dropped for the change and
    created again."""
    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    key_fields = (
        [("id", BigIntField()), ("version", IntField()), ("pk", CompositePrimaryKey("id", "version"))]
        if composite
        else [("id", BigIntField(primary_key=True, generated=False))]
    )
    operations = [
        CreateModel(name="Doc", fields=key_fields, options={"table": "clickhouse_doc"}),
        CreateModel(
            name="DocRef",
            fields=[
                ("id", BigIntField(primary_key=True, generated=False)),
                ("doc", ForeignKeyField("models.Doc", related_name="refs", on_delete=OnDelete.CASCADE)),
            ],
            options={"table": "clickhouse_doc_ref"},
        ),
        AlterField(
            model_name="DocRef",
            name="doc",
            field=ForeignKeyField("models.Doc", related_name="refs", on_delete=OnDelete.SET_NULL, null=True),
        ),
    ]
    try:
        for operation in operations:
            await operation.run("models", state, dry_run=False, state_editor=editor)
        (doc_ref,) = await DatabaseCatalog.inspect_tables(connection, ["clickhouse_doc_ref"])
    finally:
        await connection.execute_script(
            'DROP TABLE IF EXISTS "clickhouse_doc_ref"; DROP TABLE IF EXISTS "clickhouse_doc"'
        )
    columns = {column.name: column for column in doc_ref.columns}
    key_column_names = ["doc_id", "doc_version"] if composite else ["doc_id"]
    assert all(columns[column_name].nullable for column_name in key_column_names)
    assert [list(index.columns) for index in doc_ref.indexes + doc_ref.column_indexes] == [key_column_names]


@pytest.mark.asyncio
async def test_drift_reports_a_primary_key_of_other_columns(clickhouse_db):
    """The table's PRIMARY KEY was never compared with the model's primary key."""
    from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
    from hare.migrations.drift import detect_drift

    connection = Team._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Keyed",
        fields=[("id", BigIntField(primary_key=True, generated=False)), ("code", CharField(max_length=20))],
        options={"table": "clickhouse_keyed", "table_options": [ClickhouseTableOptions(order_by=("id", "code"))]},
    ).run("models", state, dry_run=False, state_editor=editor)

    def get_mismatches(result) -> list[tuple[str, str]]:
        return [(mismatch.column, mismatch.detail) for mismatch in result.mismatched_columns]

    assert get_mismatches(await detect_drift(connection, state, ["models"])) == []
    await connection.execute_script(
        'DROP TABLE "clickhouse_keyed"; CREATE TABLE "clickhouse_keyed" ("id" Int64, "code" String) '
        "ENGINE = MergeTree PRIMARY KEY (id, code) ORDER BY (id, code)"
    )
    assert get_mismatches(await detect_drift(connection, state, ["models"])) == [
        ("code", "is in the table's PRIMARY KEY, the model's primary key doesn't hold it")
    ]
    await DeleteModel(name="Keyed").run("models", state, dry_run=False, state_editor=editor)
