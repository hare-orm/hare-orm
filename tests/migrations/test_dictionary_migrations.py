"""The dictionaries a model declares in ``Meta.dictionaries`` - checked when declared, added, changed,
renamed and removed by the operations the autodetector writes, kept in the migration state, and refused
by a database without dictionaries before any SQL."""

from __future__ import annotations

from typing import Any

import pytest

from hare import Connections, fields
from hare.ddl import Dictionary, RawSQLTerm
from hare.dialects.clickhouse.schema_objects import ClickhouseDictionary
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations import (
    AddDictionary,
    AlterDictionary,
    CreateModel,
    RemoveDictionary,
    RenameDictionary,
)
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.migrations.writer import MigrationWriter
from hare.models import Model
from hare.models.enums import ModelOption

TABLE = "dictionary_country"
NAMES = ClickhouseDictionary("country_names", key=("code",), attributes=("name",))


def get_fields() -> list[tuple[str, Any]]:
    return [
        ("code", fields.CharField(max_length=2, primary_key=True)),
        ("name", fields.CharField(max_length=50)),
        ("population", fields.BigIntField(null=True)),
    ]


def get_state(model_fields: list[tuple[str, Any]] | None = None, **options: Any) -> State:
    state = State(models={}, apps=StateApps())
    CreateModel(
        name="Country", fields=model_fields or get_fields(), options={"table": TABLE, "app": "models", **options}
    ).state_forward("models", state)
    return state


def get_types(operations: list[Any]) -> list[str]:
    return [type(operation).__name__ for operation in operations]


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "", "key": ("code",), "attributes": ("name",)},
        {"name": "names", "key": (), "attributes": ("name",)},
        {"name": "names", "key": "code", "attributes": ("name",)},
        {"name": "names", "key": ("code",), "attributes": ()},
        {"name": "names", "key": ("code",), "attributes": ("name", "")},
        {"name": "names", "key": ("code",), "attributes": ("code",)},
        {"name": "names", "key": ("code",), "attributes": ("name",), "layout": "HASHED"},
        {"name": "names", "key": ("code",), "attributes": ("name",), "layout": "HASHED()); DROP TABLE x; --"},
        {"name": "names", "key": ("code",), "attributes": ("name",), "lifetime": -1},
        {"name": "names", "key": ("code",), "attributes": ("name",), "lifetime": True},
        {"name": "names", "key": ("code",), "attributes": ("name",), "lifetime": (300, 10)},
        {"name": "names", "key": ("code",), "attributes": ("name",), "lifetime": 10**10},
        {"name": "names", "key": ("code",), "attributes": ("name",), "source": "CLICKHOUSE(TABLE 't')"},
    ],
)
def test_a_wrong_dictionary_is_refused(arguments):
    with pytest.raises(ConfigurationError):
        ClickhouseDictionary(**arguments)


def test_a_dictionary_is_declared_in_meta():
    class Country(Model):
        code = fields.CharField(max_length=2, primary_key=True)
        name = fields.CharField(max_length=50)

        class Meta:
            app = "dictionary_declarations"
            dictionaries = [NAMES]

    assert Country._meta.dictionaries == (NAMES,)
    assert NAMES.get_field_names() == ("code", "name")
    assert NAMES.get_lifetime_range() == (0, 0)
    assert ClickhouseDictionary("d", key=["a"], attributes=["b"], lifetime=(10, 60)).get_lifetime_range() == (10, 60)
    path, arguments, options = ClickhouseDictionary(
        "d", key=("a",), attributes=("b",), layout="HASHED()", lifetime=60, source=RawSQLTerm("HTTP(URL 'u')")
    ).deconstruct()
    assert path.endswith("ClickhouseDictionary") and arguments == []
    assert options == {
        "name": "d",
        "key": ("a",),
        "attributes": ("b",),
        "layout": "HASHED()",
        "lifetime": 60,
        "source": RawSQLTerm("HTTP(URL 'u')"),
    }
    assert Dictionary("d", key=("a",), attributes=("b",)).deconstruct()[2] == {
        "name": "d",
        "key": ("a",),
        "attributes": ("b",),
    }
    for wrong in ([NAMES, NAMES], ["country_names"]):
        with pytest.raises(ConfigurationError):

            class Wrong(Model):
                code = fields.CharField(max_length=2, primary_key=True)

                class Meta:
                    app = "dictionary_declarations"
                    dictionaries = wrong


def test_the_autodetector_adds_changes_renames_and_removes_dictionaries():
    empty = State(models={}, apps=StateApps())
    operations = OperationGenerator(empty, get_state(dictionaries=[NAMES])).generate()
    # Added once every table of the migration exists.
    assert get_types(operations) == ["CreateModel", "AddDictionary"]
    assert "dictionaries" not in operations[0].options
    assert OperationGenerator(get_state(dictionaries=[NAMES]), get_state(dictionaries=[NAMES])).generate() == []

    cached = ClickhouseDictionary("country_names", key=("code",), attributes=("name",), lifetime=60)
    renamed = ClickhouseDictionary("names_by_code", key=("code",), attributes=("name",))
    for new_dictionaries, expected in (
        ([cached], ["AlterDictionary"]),
        ([renamed], ["RenameDictionary"]),
        ([], ["RemoveDictionary"]),
        ([NAMES, ClickhouseDictionary("people", key=("code",), attributes=("population",))], ["AddDictionary"]),
    ):
        operations = OperationGenerator(
            get_state(dictionaries=[NAMES]), get_state(dictionaries=new_dictionaries)
        ).generate()
        assert get_types(operations) == expected

    # A dictionary reading a column the migration changes is dropped before it and created after.
    people = ClickhouseDictionary("people", key=("code",), attributes=("population",))
    changed_fields = [*get_fields()[:2], ("population", fields.IntField(null=True))]
    operations = OperationGenerator(
        get_state(dictionaries=[NAMES, people]), get_state(changed_fields, dictionaries=[NAMES, people])
    ).generate()
    assert get_types(operations) == ["RemoveDictionary", "AlterField", "AddDictionary"]
    assert (operations[0].name, operations[2].dictionary) == ("people", people)


def test_the_operations_are_written_read_back_and_change_the_state():
    cached = ClickhouseDictionary("country_names", key=("code",), attributes=("name", "population"), lifetime=60)
    operations = [
        AddDictionary("Country", NAMES),
        AlterDictionary("Country", cached),
        RenameDictionary("Country", "country_names", "names_by_code"),
        RemoveDictionary("Country", "names_by_code"),
    ]
    source = MigrationWriter("0002_dictionaries", "models", operations).as_string()
    namespace: dict[str, Any] = {}
    exec(compile(source, "<migration>", "exec"), namespace)  # noqa: S102 - the test's own text
    read_back = namespace["Migration"]("0002_dictionaries", "models").operations
    assert [operation.deconstruct() for operation in read_back] == [
        operation.deconstruct() for operation in operations
    ]
    assert [operation.describe() for operation in read_back] == [operation.describe() for operation in operations]

    state = get_state()
    operations[0].state_forward("models", state)
    assert state.models[("models", "Country")].options[ModelOption.DICTIONARIES] == (NAMES,)
    assert state.apps.get_model("models.Country")._meta.dictionaries == (NAMES,)
    operations[1].state_forward("models", state)
    operations[2].state_forward("models", state)
    (renamed,) = state.models[("models", "Country")].options[ModelOption.DICTIONARIES]
    assert (renamed.name, renamed.lifetime, renamed.attributes) == ("names_by_code", 60, ("name", "population"))
    operations[3].state_forward("models", state)
    assert ModelOption.DICTIONARIES not in state.models[("models", "Country")].options
    with pytest.raises(IncompatibleStateError):
        RemoveDictionary("Country", "missing").state_forward("models", state)


@pytest.mark.asyncio
async def test_a_database_without_dictionaries_refuses_them_before_any_sql(db_simple):
    connection = Connections.get("models")
    if connection.features.supports_dictionaries:
        pytest.skip("the database has dictionaries")
    declared = get_state(dictionaries=[NAMES])
    plain = get_state()
    for operation, before in (
        (AddDictionary("Country", NAMES), plain),
        (AlterDictionary("Country", NAMES), declared),
        (RenameDictionary("Country", "country_names", "other"), declared),
        (RemoveDictionary("Country", "country_names"), declared),
    ):
        editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=True)
        after = before.clone()
        operation.state_forward("models", after)
        with pytest.raises(UnSupportedError, match="not supported on"):
            await operation.database_forward("models", before, after, editor)
        assert editor.collected_sql == []
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=True)
    with pytest.raises(UnSupportedError, match="not supported on"):
        editor.table_creation.get_schema_objects_after_table_sqls(declared.apps.get_model("models.Country"))
