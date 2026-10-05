"""ClickHouse dictionaries - rows of a model's table kept in the server's memory: created with the
model, read by ``DictGet``, loaded again on demand, added, replaced, renamed and removed by migrations,
and compared by drift."""

import os

import pytest

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.fields import UInt64Field
from hare.dialects.clickhouse.functions import DictGet
from hare.dialects.clickhouse.schema_objects import ClickhouseDictionary
from hare.exceptions import ConfigurationError, QueryError
from hare.migrations.drift import detect_drift
from hare.migrations.operations import (
    AddDictionary,
    AlterDictionary,
    CreateModel,
    DeleteModel,
    RemoveDictionary,
    RenameDictionary,
)
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.query.expressions import F, Value
from tests.dialects.clickhouse.models import Country, Player, Team

NAME = fields.CharField(max_length=50)


async def get_dictionaries(connection, prefix):
    # A dictionary shows its layout and its lifetime once it is loaded - with its first read.
    names = await connection.execute_dicts(
        f"SELECT name FROM system.dictionaries WHERE database = currentDatabase() AND name LIKE '{prefix}%'"
    )
    for row in names:
        await connection.execute_script(f"SYSTEM RELOAD DICTIONARY {row['name']}")
    rows = await connection.execute_dicts(
        "SELECT name, type, key.names AS key_names, attribute.names AS attribute_names, lifetime_min, lifetime_max "
        f"FROM system.dictionaries WHERE database = currentDatabase() AND name LIKE '{prefix}%' ORDER BY name"
    )
    return {row["name"]: tuple(list(row.values())[1:]) for row in rows}


async def run(operation, state, editor):
    await operation.run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_a_dictionary_is_created_with_the_model_and_read_by_its_key(clickhouse_db):
    connection = Country._meta.connection
    assert await get_dictionaries(connection, "country_") == {
        "country_names": ("ComplexKeyHashed", ["code"], ["title", "population"], 0, 300)
    }
    await Country.objects.bulk_create(
        [Country(code="fr", name="France", population=68), Country(code="de", name="Germany", population=None)]
    )
    await Country.objects.reload_dictionary("country_names")
    team = await Team.objects.create(name="fr")
    await Player.objects.bulk_create(
        [
            Player(id=1, name="fr", team=team, rating=1),
            Player(id=2, name="de", team=team, rating=2),
            Player(id=3, name="xx", rating=3),
        ]
    )
    country = DictGet("country_names", "title", "name", output_field=NAME)
    rows = await Player.objects.annotate(country=country).order_by("rating").values_list("name", "country")
    # A key the dictionary doesn't hold reads as the attribute type's own default.
    assert rows == [("fr", "France"), ("de", "Germany"), ("xx", "")]
    assert await Player.objects.annotate(country=country).filter(country="France").count() == 1
    people = DictGet("country_names", "population", F("name"), output_field=fields.BigIntField(null=True), default=-1)
    assert await Player.objects.annotate(people=people).order_by("rating").values_list("people", flat=True) == [
        68,
        None,
        -1,
    ]
    # A key read through a relation; a NULL key - of a row without the relation - reads NULL.
    by_team = DictGet("country_names", "title", "team__name", output_field=NAME, default=Value("none"))
    assert await Player.objects.annotate(country=by_team).order_by("rating").values_list("country", flat=True) == [
        "France",
        "France",
        None,
    ]
    absent = DictGet("country_names", "title", Value("zz"), output_field=NAME, default=Value("none"))
    assert await Player.objects.annotate(country=absent).filter(id=1).values_list("country", flat=True) == ["none"]

    # A row written after the dictionary was loaded is read through it once it is loaded again.
    await Country.objects.create(code="xx", name="Nowhere")
    assert await Player.objects.annotate(country=country).filter(name="xx").values_list("country", flat=True) == [""]
    await Country.objects.reload_dictionary("country_names")
    assert await Player.objects.annotate(country=country).filter(name="xx").values_list("country", flat=True) == [
        "Nowhere"
    ]
    with pytest.raises(QueryError):
        await Country.objects.reload_dictionary("missing")


def test_dict_get_is_checked():
    for arguments in (
        ("", "title", "name"),
        ("country_names", "", "name"),
        ("country_names", "title", ()),
    ):
        with pytest.raises(QueryError):
            DictGet(*arguments, output_field=NAME)
    with pytest.raises(QueryError):
        DictGet("country_names", "title", "name", output_field="CharField")


@pytest.mark.asyncio
async def test_migrations_add_replace_rename_and_remove_dictionaries(clickhouse_db):
    connection = Country._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await run(
        CreateModel(
            name="Airport",
            fields=[
                ("id", UInt64Field(primary_key=True, generated=False)),
                ("country", fields.CharField(max_length=2)),
                ("code", fields.CharField(max_length=3)),
                ("city", fields.CharField(max_length=30)),
            ],
            options={"table": "dict_airport"},
        ),
        state,
        editor,
    )
    by_code = ClickhouseDictionary("dict_cities", key=("country", "code"), attributes=("city",), lifetime=(10, 60))
    by_number = ClickhouseDictionary("dict_by_number", key=("id",), attributes=("city", "code"), layout="HASHED()")
    try:
        await connection.execute_script(
            "INSERT INTO dict_airport VALUES (1, 'fr', 'cdg', 'Paris'), (2, 'de', 'ber', 'Berlin')"
        )
        await run(AddDictionary("Airport", by_code), state, editor)
        await run(AddDictionary("Airport", by_number), state, editor)
        assert await get_dictionaries(connection, "dict_") == {
            "dict_by_number": ("Hashed", ["id"], ["city", "code"], 0, 0),
            "dict_cities": ("ComplexKeyHashed", ["country", "code"], ["city"], 10, 60),
        }
        await Player.objects.create(id=1, name="cdg", rating=1)
        city = DictGet("dict_cities", "city", [Value("fr"), "name"], output_field=fields.CharField(max_length=30))
        assert await Player.objects.annotate(city=city).values_list("city", flat=True) == ["Paris"]
        by_rating = DictGet("dict_by_number", "city", "rating", output_field=fields.CharField(max_length=30))
        # A layout keyed by one number takes the key alone, not in a tuple.
        assert await Player.objects.annotate(city=by_rating).values_list("city", flat=True) == ["Paris"]

        replaced = ClickhouseDictionary("dict_cities", key=("code",), attributes=("city", "country"), lifetime=5)
        await run(AlterDictionary("Airport", replaced), state, editor)
        await run(RenameDictionary("Airport", "dict_by_number", "dict_numbers"), state, editor)
        assert await get_dictionaries(connection, "dict_") == {
            "dict_cities": ("ComplexKeyHashed", ["code"], ["city", "country"], 0, 5),
            "dict_numbers": ("Hashed", ["id"], ["city", "code"], 0, 0),
        }
        by_code_alone = DictGet("dict_cities", "country", "name", output_field=fields.CharField(max_length=2))
        assert await Player.objects.annotate(country=by_code_alone).values_list("country", flat=True) == ["fr"]
        await run(RemoveDictionary("Airport", "dict_numbers"), state, editor)
        assert list(await get_dictionaries(connection, "dict_")) == ["dict_cities"]
    finally:
        # The dictionaries a model declares go with its table.
        await run(DeleteModel(name="Airport"), state, editor)
    assert await get_dictionaries(connection, "dict_") == {}


@pytest.mark.asyncio
async def test_a_source_is_declared_with_its_secrets_left_out(clickhouse_db, monkeypatch):
    connection = Country._meta.connection
    shown = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=True)
    (statement,) = shown.dictionaries.get_dictionary_create_sqls(Country, Country._meta.dictionaries[0])
    # The SQL shown, not run, has no password in it.
    assert connection.password not in statement and "PASSWORD '[HIDDEN]'" in statement
    assert '("code" VARCHAR(2), "title" VARCHAR(50), "population" Nullable(BIGINT)) PRIMARY KEY "code"' in statement
    assert "LAYOUT(COMPLEX_KEY_HASHED()) LIFETIME(MIN 0 MAX 300)" in statement

    external = ClickhouseDictionary(
        "country_external",
        key=("code",),
        attributes=("name",),
        source=RawSQLTerm(
            "CLICKHOUSE(TABLE 'country' USER '{env:HARE_TEST_DICTIONARY_USER}' "
            "PASSWORD '{env:HARE_TEST_DICTIONARY_PASSWORD}')"
        ),
    )
    assert "HARE_TEST_DICTIONARY_PASSWORD" in repr(external.deconstruct())
    monkeypatch.delenv("HARE_TEST_DICTIONARY_PASSWORD", raising=False)
    monkeypatch.setenv("HARE_TEST_DICTIONARY_USER", connection.user)
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    with pytest.raises(ConfigurationError, match="HARE_TEST_DICTIONARY_PASSWORD"):
        editor.dictionaries.get_dictionary_create_sqls(Country, external)
    monkeypatch.setenv("HARE_TEST_DICTIONARY_PASSWORD", connection.password)
    assert (
        os.environ["HARE_TEST_DICTIONARY_PASSWORD"]
        in editor.dictionaries.get_dictionary_create_sqls(Country, external)[0]
    )
    await editor.dictionaries.create_dictionary(Country, external)
    try:
        await Country.objects.create(code="it", name="Italy")
        await editor.dictionaries.reload_dictionary(Country, external)
        rows = await connection.execute_dicts("SELECT dictGet('country_external', 'title', 'it') AS name")
        assert rows[0]["name"] == "Italy"
    finally:
        await editor.dictionaries.drop_dictionary(Country, external)
    for wrong in (
        ClickhouseDictionary("wrong", key=("code",), attributes=("missing",)),
        ClickhouseDictionary("wrong", key=("missing",), attributes=("name",)),
    ):
        with pytest.raises(ConfigurationError, match="names no field"):
            editor.dictionaries.get_dictionary_create_sqls(Country, wrong)


@pytest.mark.asyncio
async def test_drift_finds_no_change_of_a_declared_dictionary_and_a_changed_one(clickhouse_db):
    connection = Country._meta.connection
    state = State(models={}, apps=StateApps())
    state.models[("models", "Country")] = ModelState.make_from_model("models", Country)

    async def get_dictionary_operations():
        drift = await detect_drift(connection, state, ["models"])
        return [
            (type(operation).__name__, operation.dictionary)
            for operation in drift.operations
            if "Dictionary" in type(operation).__name__
        ]

    assert await get_dictionary_operations() == []
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    (declared,) = Country._meta.dictionaries
    changed = ClickhouseDictionary(
        "country_names", key=("code",), attributes=("name",), lifetime=(5, 10), layout="COMPLEX_KEY_SPARSE_HASHED()"
    )
    try:
        await editor.dictionaries.alter_dictionary(Country, declared, changed)
        # The operation bringing the database back to the declaration.
        assert await get_dictionary_operations() == [("AlterDictionary", declared)]
        await editor.dictionaries.drop_dictionary(Country, changed)
        assert await get_dictionary_operations() == [("AddDictionary", declared)]
    finally:
        await editor.dictionaries.drop_model_dictionaries(Country)
        await editor.dictionaries.create_dictionary(Country, declared)
    assert await get_dictionary_operations() == []
