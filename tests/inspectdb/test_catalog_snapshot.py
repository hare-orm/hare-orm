import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hare.ddl.enums import GrantTarget
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.database_function import DatabaseFunction
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.introspection.constants import (
    POSTGRESQL_CATALOG_REFERENCE_CASTS,
    POSTGRESQL_TABLE_CATALOG_QUERIES,
)
from hare.dialects.postgresql.introspection.postgresql_catalog_parts import PostgresqlCatalogParts
from hare.dialects.postgresql.introspection.postgresql_catalog_reader import PostgresqlCatalogReader
from hare.dialects.postgresql.introspection.postgresql_declaration_mapper import PostgresqlDeclarationMapper
from hare.dialects.postgresql.introspection.postgresql_definitions import PostgresqlDefinitions
from hare.dialects.postgresql.introspection.postgresql_object_grants import PostgresqlObjectGrants
from hare.dialects.sqlite.introspection.sqlite_column_declarations import SqliteColumnDeclarations
from hare.dialects.sqlite.introspection.sqlite_declaration_mapper import SqliteDeclarationMapper
from hare.dialects.sqlite.sqlite_introspector import SqliteIntrospector
from hare.fields import IntField
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.inspectdb.introspection.catalog_identities import CatalogIdentities
from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot
from hare.inspectdb.introspection.catalog_sql import CatalogSql
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.enums import CatalogDiagnosticCode, SchemaPartType
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.inspectdb.introspection.table_info import TableInfo
from hare.inspectdb.schema_inspector import SchemaInspector
from hare.migrations.drift.drift_result import DriftResult
from hare.migrations.drift.shadow.schema_comparison import SchemaComparison
from hare.migrations.operations import CreateModel
from hare.migrations.state.state import State

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
@pytest.mark.parametrize("representation", ["decoded", "text", "bytes"])
async def test_catalog_reader_decodes_sequence_metadata_before_freezing(representation):
    sequences = [{"name": "book_id_seq", "dependency_type": "i", "start": 5}]
    encoded = sequences if representation == "decoded" else json.dumps(sequences)
    if representation == "bytes":
        encoded = encoded.encode()
    rows = {"columns": [{"table_name": "book", "column_name": "id", "column_sequences": encoded}]}
    connection = SimpleNamespace(
        execute_dicts=AsyncMock(
            side_effect=[
                [{"table_name": "book"}],
                *[rows.get(section, []) for section in POSTGRESQL_TABLE_CATALOG_QUERIES],
            ]
        )
    )
    snapshot = await PostgresqlCatalogReader.read_tables(connection, ["book"], "public")
    assert snapshot.copy_records()["columns"][0]["column_sequences"] == sequences
    sequences[0]["start"] = 9
    assert snapshot.copy_records()["columns"][0]["column_sequences"][0]["start"] == 5


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("language", "library", "from_table", "reconstructable"),
    [
        ("plpgsql", None, "", True),
        ("c", None, "", False),
        ("c", "/trigger.so", "", False),
        ("plpgsql", "/trigger.so", "", False),
        ("CustomLang", None, "", False),
        ("custom language", None, "", False),
        ("plpgsql", None, "FROM public.other_book ", False),
    ],
)
def test_trigger_catalog_attributes_preserve_body_or_diagnostic(
    enabled, language, library, from_table, reconstructable
):
    body = "NEW.note := '$function$ stays literal'; RETURN NEW;"
    row = {
        "table_name": "book",
        "name": "book_changed",
        "trigger_def": (
            "CREATE CONSTRAINT TRIGGER book_changed AFTER UPDATE ON public.book "
            f"{from_table}DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.book_changed_fn()"
        ),
        "body": f"BEGIN\n{body}\nEND;",
        "language": language,
        "library": library,
        "enabled": enabled,
    }
    triggers, unparsed = PostgresqlDeclarationMapper.build_triggers([row])
    if reconstructable:
        assert unparsed == []
        assert len(triggers) == 1
        trigger = triggers[0]
        assert trigger.body.sql == body
        assert trigger.language is None
        assert trigger.enabled is enabled
        assert trigger.deferrable and trigger.initially_deferred
    else:
        assert triggers == []
        assert unparsed == [(row["name"], row["trigger_def"])]
    parts = PostgresqlCatalogParts.get(
        CatalogSnapshot("public", ("book",), {"triggers": [row]}),
        [TableInfo("book", triggers=triggers)],
    )
    part = next(part for part in parts if part.part_type is SchemaPartType.TRIGGER)
    body_key = "body" if language in ("sql", "plpgsql") else "literal_body"
    assert part.definition[body_key] == row["body"]
    assert part.definition["library"] == library


@pytest.mark.parametrize(
    "include,expected",
    [("plain", ("plain",)), ('"a,b", "a""b"', ("a,b", 'a"b')), ("lower(name)", None), ("schema.name", None)],
)
def test_exclusion_include_preserves_catalog_identifiers(include, expected):
    constraint = PostgresqlDefinitions.parse_exclusion_constraint(
        "book_period", f"EXCLUDE USING gist (period WITH &&) INCLUDE ({include})"
    )
    if expected is None:
        assert constraint is None
    else:
        assert constraint.include == expected


@pytest.mark.parametrize(
    ("property_name", "changed_value"),
    [
        ("start", 3),
        ("increment", 2),
        ("minimum", -1),
        ("maximum", 100),
        ("cache", 20),
        ("cycle", True),
        ("type", "bigint"),
    ],
)
@pytest.mark.parametrize("change_comment", [False, True])
def test_identity_sequence_drift_does_not_fabricate_a_field_repair(property_name, changed_value, change_comment):
    sequence = {
        "name": "book_number_seq",
        "schema": "public",
        "type": "integer",
        "start": 1,
        "increment": 1,
        "minimum": 1,
        "maximum": 2147483647,
        "cache": 1,
        "cycle": False,
        "dependency_type": "i",
    }
    shadow_row = {"table_name": "book", "column_name": "number", "column_sequences": [sequence]}
    real_row = {**shadow_row, "column_sequences": [{**sequence, property_name: changed_value}]}
    real_column = ColumnInfo("number", "integer", False, False, False, description="old comment")
    shadow_column = ColumnInfo(
        "number",
        "integer",
        False,
        False,
        False,
        description="new comment" if change_comment else "old comment",
    )
    shadow_row["description"] = shadow_column.description
    real_row["description"] = real_column.description
    shadow = PostgresqlCatalogParts.get(
        CatalogSnapshot("public", ("book",), {"columns": [shadow_row]}),
        [TableInfo("book", columns=[shadow_column])],
    )
    real = PostgresqlCatalogParts.get(
        CatalogSnapshot("public", ("book",), {"columns": [real_row]}),
        [TableInfo("book", columns=[real_column])],
    )
    state = State()
    CreateModel("Book", [("number", IntField(description=shadow_column.description))]).state_forward("models", state)
    comparison = SchemaComparison(state, None, real, shadow)
    comparison.compare_columns(("models", "Book"), state.models["models", "Book"], "book", {"number": "number"})
    assert len(comparison.mismatched_objects) == 1
    assert len(comparison.operations) == int(change_comment)


@pytest.mark.parametrize("dependency_type", ["i", "a"])
def test_sequence_identity_uses_column_binding_only_for_internal_sequences(dependency_type):
    sequence = {"name": "first", "schema": "public", "cache": 1, "dependency_type": dependency_type}
    snapshot = CatalogSnapshot(
        "public",
        ("book",),
        {
            "columns": [
                {"table_name": "book", "column_name": "number", "column_sequences": [sequence]},
            ]
        },
    )
    original = snapshot.copy_records()["columns"][0]
    renamed = {**original, "column_sequences": [{**sequence, "name": "second", "schema": "shadow"}]}
    first = PostgresqlCatalogParts.get_column_definition(original)
    second = PostgresqlCatalogParts.get_column_definition(renamed)
    assert (first == second) is (dependency_type == "i")
    assert snapshot.copy_records()["columns"][0] == original


@pytest.mark.parametrize("generated_key,uses_default", [(True, True), (False, True), (True, False)])
def test_serial_key_sequence_name_is_normalized_only_for_its_exact_owned_default(generated_key, uses_default):
    column = ColumnInfo("id", "integer", False, generated_key, False)
    sequence = {
        "name": "before_id_seq",
        "schema": "public",
        "cache": 1,
        "dependency_type": "a",
        "is_column_default": uses_default,
    }
    original = {"column_default": "nextval('before_id_seq'::regclass)", "column_sequences": [sequence]}
    renamed = {
        "column_default": "nextval('after_id_seq'::regclass)",
        "column_sequences": [{**sequence, "name": "after_id_seq"}],
    }
    first = PostgresqlCatalogParts.get_column_definition(original, column)
    second = PostgresqlCatalogParts.get_column_definition(renamed, column)
    assert (first == second) is (generated_key and uses_default)
    altered = {**original, "column_sequences": [{**sequence, "cache": 20}]}
    assert first != PostgresqlCatalogParts.get_column_definition(altered, column)
    assert original["column_sequences"][0]["name"] == "before_id_seq"


def test_catalog_identity_mapping_preserves_literals_and_compares_quoted_schema_references():
    identities = CatalogIdentities({"shadow": "select"}, POSTGRESQL_CATALOG_REFERENCE_CASTS)
    shadow_sql = "SELECT shadow.item.id, 'shadow.item', $$shadow.item$$ /* shadow.item */ FROM shadow.item"
    real_sql = 'SELECT "select".item.id, \'shadow.item\', $$shadow.item$$ /* shadow.item */ FROM "select".item'
    assert identities.map_sql(shadow_sql) == identities.map_sql(real_sql)
    column = SchemaPart.of(
        SchemaPartType.COLUMN,
        "item",
        "name",
        {
            "description": "shadow",
            "db_default": "shadow.item",
            "enum_type_schema": "shadow",
            "generated_expression": "shadow.f(1)",
            "sequence_default": SqlDefault("nextval('shadow.seq'::regclass)"),
        },
    )
    mapped = identities.map_part(column)
    assert mapped.definition["description"] == "shadow"
    assert mapped.definition["db_default"] == "shadow.item"
    assert mapped.definition["enum_type_schema"] == "select"
    assert mapped.definition["generated_expression"] == '"select".f(1)'
    assert mapped.definition["sequence_default"][1] == ["nextval('\"select\".seq'::regclass)"]
    comment = SchemaPart.of(SchemaPartType.COMMENT, "item", "item", "shadow.item")
    assert identities.map_part(comment) is comment


class SqliteCatalogConnection:
    dialect = DialectRegistry.get_dialect("sqlite")

    def __init__(self, database):
        self.database = database
        self.query_count = 0

    async def execute_dicts(self, sql, parameters=None):
        self.query_count += 1
        cursor = self.database.execute(sql, parameters or [])
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    async def execute(self, sql, parameters=None):
        self.query_count += 1
        rows = self.database.execute(sql, parameters or []).fetchall()
        return len(rows), rows


@pytest.mark.parametrize(
    "expression",
    [
        "f('a,b', ')')",
        "f($body$a,)b$body$)",
        "f('a||b', ')')",
        'f(1 /* ) , */), "odd,name"',
        "f(`odd,``name`)",
    ],
)
@pytest.mark.parametrize("separator", [",", "||"])
def test_catalog_delimiters_inside_literals_identifiers_and_comments_are_opaque(expression, separator):
    # The third expression contains one actual top-level comma.
    expected = (
        ["f(1 /* ) , */)", ' "odd,name"'] if separator == "," and expression.startswith("f(1 ") else [expression]
    )
    assert CatalogSql.split_terms(expression + separator + " next_column", separator) == [*expected, " next_column"]
    inner = "'a)b', $tag$x)y$tag$, 1 /* ) */"
    assert CatalogSql.extract_parentheses(f"({inner}) suffix", 0) == inner


@pytest.mark.asyncio
async def test_sqlite_catalog_can_be_mapped_repeatedly_without_io_or_mutating_its_records():
    database = sqlite3.connect(":memory:")
    try:
        database.executescript("""
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE item (
                id INTEGER PRIMARY KEY, title TEXT DEFAULT 'x,y',
                parent_id INTEGER REFERENCES parent(id),
                derived TEXT GENERATED ALWAYS AS (substr(title, 1, 2) || ')') STORED,
                CHECK (length(title) > 0)
            );
            CREATE INDEX "odd,index" ON item(substr(title, 1, 2), title) WHERE title != 'a,b';
            CREATE TRIGGER audit_update AFTER UPDATE ON item BEGIN SELECT 'comma, paren)'; END;
        """)
        connection = SqliteCatalogConnection(database)
        snapshot = await DatabaseCatalog.read_tables(connection, ["item"], "main")
        query_count = connection.query_count
        mapper = SqliteDeclarationMapper(SqliteIntrospector, connection.dialect.table_options_class)
        table = mapper.get_tables(snapshot)[0]
        assert table.foreign_keys["parent_id"].target_table == "parent"
        assert table.indexes[0].expression_terms == ["substr(title, 1, 2)", "title"]
        assert table.columns[-1].generated_expression == "substr(title, 1, 2) || ')'"
        original_sql = snapshot.records["tables"][0]["table_rows"][0]["sql"]
        assert "substr(title, 1, 2) || ')'" in original_sql
        with pytest.raises(TypeError):
            snapshot.records["tables"][0]["columns"][0]["name"] = "changed"
        table.columns.clear()
        assert len(mapper.get_tables(snapshot)[0].columns) == 4
        assert connection.query_count == query_count
    finally:
        database.close()


@pytest.mark.asyncio
async def test_unsupported_relation_has_a_typed_diagnostic_and_preserves_catalog_records():
    database = sqlite3.connect(":memory:")
    try:
        database.executescript("""
            CREATE TABLE parent (a INT, b INT, PRIMARY KEY (a, b));
            CREATE TABLE child (id INTEGER PRIMARY KEY, x INT, y INT,
                FOREIGN KEY (x, y) REFERENCES parent(a, b) ON DELETE RESTRICT);
        """)
        connection = SqliteCatalogConnection(database)
        snapshot = await DatabaseCatalog.read_tables(connection, ["child"], "main")
        table = SqliteDeclarationMapper(SqliteIntrospector, connection.dialect.table_options_class).get_tables(
            snapshot
        )[0]
        diagnostics = table.get_diagnostics(CatalogDiagnosticCode.FOREIGN_KEY)
        assert len(diagnostics) == 1
        assert diagnostics[0].columns == ("x", "y")
        assert {row["on_delete"] for row in snapshot.records["tables"][0]["foreign_keys"]} == {"RESTRICT"}
        source = SchemaInspector.render_module([table], "sqlite")
        assert "composite foreign key" in source and "(x, y)" in source
        assert {column.name for column in table.columns} == {"id", "x", "y"}
    finally:
        database.close()


@pytest.mark.parametrize(
    ("first_sql", "second_sql", "part_type", "name"),
    [
        (
            "CREATE INDEX title_index ON item(title COLLATE BINARY)",
            "CREATE INDEX title_index ON item(title COLLATE NOCASE)",
            SchemaPartType.INDEX,
            "title_index",
        ),
        (
            "CREATE INDEX title_index ON item(title ASC)",
            "CREATE INDEX title_index ON item(title DESC)",
            SchemaPartType.INDEX,
            "title_index",
        ),
        (
            "CREATE TABLE item(id INTEGER PRIMARY KEY, title TEXT)",
            "CREATE TABLE item(id INTEGER PRIMARY KEY DESC, title TEXT)",
            SchemaPartType.COLUMN,
            "id",
        ),
        (
            "CREATE TABLE item(id INT, title TEXT)",
            "CREATE TABLE item(id INT NOT NULL, title TEXT)",
            SchemaPartType.COLUMN,
            "id",
        ),
    ],
)
@pytest.mark.asyncio
async def test_catalog_comparison_retains_storage_properties(first_sql, second_sql, part_type, name):
    definitions = []
    for sql in (first_sql, second_sql):
        database = sqlite3.connect(":memory:")
        try:
            if sql.startswith("CREATE INDEX"):
                database.execute("CREATE TABLE item(id INTEGER PRIMARY KEY, title TEXT)")
            database.execute(sql)
            connection = SqliteCatalogConnection(database)
            snapshot = await DatabaseCatalog.read_tables(connection, ["item"], "main")
            tables = SqliteIntrospector.get_table_infos(snapshot, connection.dialect)
            parts = SqliteIntrospector.get_table_parts(snapshot, tables)
            definitions.append(next(part.definition for part in parts if part.key == (part_type, "item", name)))
        finally:
            database.close()
    assert definitions[0] != definitions[1]


@pytest.mark.parametrize("overlap", ["single", "mixed", "composite"])
def test_declaration_mappers_preserve_overlapping_foreign_keys_as_diagnostics(overlap):
    columns = {
        "single": [("parent_id",), ("parent_id",)],
        "mixed": [("parent_a", "parent_b"), ("parent_a",)],
        "composite": [("parent_a", "parent_b"), ("parent_a", "parent_b")],
    }[overlap]
    sqlite_rows, postgres_rows = [], []
    for constraint_id, names in enumerate([*columns, ("other_id",)]):
        targets = ("a", "b") if len(names) == 2 else ("id",)
        target = "parent" if len(names) == 2 else "author"
        sqlite_rows.extend(
            {
                "id": constraint_id,
                "seq": position,
                "from": name,
                "to": destination,
                "table": target,
                "on_delete": "CASCADE",
                "on_update": "NO ACTION",
            }
            for position, (name, destination) in enumerate(zip(names, targets, strict=True))
        )
        postgres_rows.append(
            {
                "conname": f"item_fk_{constraint_id}",
                "columns": names,
                "target_columns": targets,
                "target_primary_key_columns": targets,
                "target_table": target,
                "target_schema": "public",
                "delete_rule": "CASCADE",
                "update_rule": "NO ACTION",
                "is_deferrable": False,
                "is_initially_deferred": False,
            }
        )
    sqlite_result = SqliteDeclarationMapper.get_foreign_keys(
        sqlite_rows,
        {"parent": [{"name": "a", "pk": 1}, {"name": "b", "pk": 2}], "author": [{"name": "id", "pk": 1}]},
        "item",
        {0: None, 1: None, 2: None},
    )
    postgres_result = PostgresqlDeclarationMapper.build_foreign_keys(postgres_rows, "public")
    for simple, composite, diagnostics in (sqlite_result, postgres_result):
        assert set(simple) == {"other_id"}
        assert composite == []
        assert diagnostics == [(f"item_fk_{position}", names) for position, names in enumerate(columns)]


@pytest.mark.asyncio
async def test_catalog_comparison_preserves_all_foreign_keys_on_the_same_columns():
    database = sqlite3.connect(":memory:")
    try:
        database.executescript("""
            CREATE TABLE parent(a INT, b INT, PRIMARY KEY(a,b));
            CREATE TABLE item(id INTEGER PRIMARY KEY, x INT, y INT, title TEXT,
                CONSTRAINT title_unique UNIQUE(title),
                FOREIGN KEY(x,y) REFERENCES parent(a,b) ON DELETE RESTRICT,
                FOREIGN KEY(x,y) REFERENCES parent(a,b) ON DELETE CASCADE);
        """)
        connection = SqliteCatalogConnection(database)
        snapshot = await DatabaseCatalog.read_tables(connection, ["item"], "main")
        tables = SqliteIntrospector.get_table_infos(snapshot, connection.dialect)
        parts = {part.key: part for part in SqliteIntrospector.get_table_parts(snapshot, tables)}
        foreign_keys = parts[SchemaPartType.FOREIGN_KEY, "item", "x,y"].definition
        assert {item["on_delete"] for item in foreign_keys} == {"RESTRICT", "CASCADE"}
        assert (SchemaPartType.INDEX, "item", "title_unique") in parts
        assert not any(key[2].startswith("sqlite_autoindex") for key in parts)
    finally:
        database.close()


def test_column_clauses_ignore_literals_comments_and_nested_collations():
    database = sqlite3.connect(":memory:")
    try:
        database.execute("""CREATE TABLE item (
            "odd,name" TEXT COLLATE NOCASE DEFAULT 'COLLATE RTRIM',
            derived TEXT GENERATED ALWAYS AS /* AS (broken) */
                (substr("odd,name" COLLATE RTRIM, 1, 2) || '), AS (') STORED
        )""")
        sql = database.execute("SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0]
        columns = SqliteColumnDeclarations.get_columns(sql, {"odd,name", "derived"})
        assert SqliteColumnDeclarations.get_collation(columns["odd,name"]) == "nocase"
        assert SqliteColumnDeclarations.get_collation(columns["derived"]) == "binary"
        assert (
            SqliteColumnDeclarations.get_generated_expression(columns["derived"])
            == "substr(\"odd,name\" COLLATE RTRIM, 1, 2) || '), AS ('"
        )
    finally:
        database.close()


@pytest.mark.parametrize("table_constraint", [False, True])
@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        ("", ""),
        ("DEFERRABLE INITIALLY IMMEDIATE", "DEFERRABLE INITIALLY IMMEDIATE"),
        ("DEFERRABLE INITIALLY DEFERRED", "DEFERRABLE INITIALLY DEFERRED"),
        ("NOT DEFERRABLE INITIALLY DEFERRED", ""),
    ],
)
@pytest.mark.asyncio
async def test_foreign_key_catalog_timing_matches_constraint_enforcement(table_constraint, clause, expected):
    database = sqlite3.connect(":memory:")
    try:
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        reference = f"REFERENCES parent(id) ON DELETE CASCADE {clause}"
        declaration = (
            f"parent_id INT, FOREIGN KEY(parent_id) {reference}" if table_constraint else f"parent_id INT {reference}"
        )
        database.execute(f"CREATE TABLE item(id INTEGER PRIMARY KEY, {declaration})")
        connection = SqliteCatalogConnection(database)
        snapshot = await DatabaseCatalog.read_tables(connection, ["item"], "main")
        tables = SqliteIntrospector.get_table_infos(snapshot, connection.dialect)
        assert tables[0].foreign_keys["parent_id"].deferral == expected
        part = next(
            part
            for part in SqliteIntrospector.get_table_parts(snapshot, tables)
            if part.part_type is SchemaPartType.FOREIGN_KEY
        )
        assert part.definition[0]["deferral"] == expected
        database.execute("BEGIN")
        if expected == "DEFERRABLE INITIALLY DEFERRED":
            database.execute("INSERT INTO item(parent_id) VALUES (999)")
            with pytest.raises(sqlite3.IntegrityError):
                database.commit()
        else:
            with pytest.raises(sqlite3.IntegrityError):
                database.execute("INSERT INTO item(parent_id) VALUES (999)")
    finally:
        database.close()


def test_unrepresentable_privileges_remain_drift_without_a_fabricated_operation():
    snapshot = CatalogSnapshot(
        "public",
        ("item",),
        {
            "table_privileges": [
                {"table_name": "item", "role": "reader", "privilege_type": "FUTURE_PRIVILEGE", "is_grantable": True}
            ]
        },
    )
    parts = PostgresqlCatalogParts.get(snapshot, [TableInfo("item")])
    comparison = SchemaComparison(None, None, parts, [])
    comparison.compare_grants("Item", "item")
    assert comparison.operations == []
    assert len(comparison.mismatched_objects) == 1
    result = DriftResult([], [], [], mismatched_objects=comparison.mismatched_objects)
    assert result.has_drift


def test_grants_to_separate_function_overloads_are_not_merged_by_name():
    rows = [
        {
            "object_name": "read_value",
            "arguments": arguments,
            "role": "reader",
            "privilege_type": "EXECUTE",
            "is_grantable": False,
        }
        for arguments in ("value integer", "value text")
    ]
    parts = PostgresqlObjectGrants.get_parts(rows, GrantTarget.FUNCTION)
    assert len(parts) == 2
    assert {part.definition["arguments"] for part in parts} == {"value integer", "value text"}
    comparison = SchemaComparison(None, None, parts, [])
    model = SimpleNamespace(_meta=SimpleNamespace(sequences=(), functions=(SimpleNamespace(name="read_value"),)))
    comparison.compare_object_grants(model, "Item")
    assert comparison.operations == []
    assert len(comparison.mismatched_objects) == 2


@pytest.mark.parametrize(
    "signatures", [None, (), ("value integer",), ("value text",), ("value integer", "value text")]
)
def test_inspectdb_grants_require_complete_unambiguous_function_inventory(signatures):
    rows = [
        {
            "object_name": "read_value",
            "arguments": "value integer",
            "role": "reader",
            "privilege_type": "EXECUTE",
            "is_grantable": True,
        }
    ]
    parts = PostgresqlObjectGrants.get_parts(
        rows,
        GrantTarget.FUNCTION,
        function_signatures=None if signatures is None else {"read_value": signatures},
    )
    declaration = DatabaseFunction("read_value", "integer", RawSQLTerm("SELECT value"), arguments=("value integer",))
    note = SchemaInspector.get_unattached_object_note(declaration, parts)
    assert (parts[0].value is not None) is (signatures == ("value integer",))
    assert ("# TODO: grant" in note) is (signatures != ("value integer",))
    assert "reader" in note
    assert "value integer" in note
    assert "with_grant_option" in note


def test_grant_identity_remapping_preserves_its_logical_namespace():
    definition = {"arguments": "shadow.value", "role": "reader"}
    part = SchemaPart(SchemaPartType.GRANT, "", "extra." + repr(definition), definition)
    mapped = CatalogIdentities({"shadow": "public"}).map_part(part)
    assert mapped.definition == {"arguments": '"public".value', "role": "reader"}
    assert mapped.name == "extra." + repr(mapped.definition)


@pytest.mark.parametrize("actual_signatures", [("value integer",), ("value text",), ("value integer", "value text")])
def test_function_drift_only_suggests_replacing_an_unambiguous_matching_signature(actual_signatures):
    declaration = DatabaseFunction("read_value", "integer", RawSQLTerm("SELECT value"), arguments=("value integer",))
    model = SimpleNamespace(_meta=SimpleNamespace(functions=(declaration,)))
    actual = SchemaPart(
        SchemaPartType.FUNCTION, "", "read_value", {"body": "SELECT 0"}, operation_signatures=actual_signatures
    )
    expected = SchemaPart(
        SchemaPartType.FUNCTION, "", "read_value", {"body": "SELECT value"}, operation_signatures=("value integer",)
    )
    comparison = SchemaComparison(None, None, [actual], [expected])
    comparison.compare_named_objects(SchemaPartType.FUNCTION, model, "Item", "")
    if actual_signatures == expected.operation_signatures:
        assert [type(operation).__name__ for operation in comparison.operations] == ["AlterFunction"]
        assert comparison.mismatched_objects == []
    else:
        assert comparison.operations == []
        assert len(comparison.mismatched_objects) == 1
