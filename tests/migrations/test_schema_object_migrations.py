"""Migrations of what a model declares beside its table - views, materialized views, functions,
sequences, row level security, policies and grants: the declarations and Meta options refusing wrong
values, the autodetector's operations and their order around the other operations, the operations
written to a migration file and read back, the state they leave, the SQL sqlmigrate shows, every
operation applied and unapplied on PostgreSQL, and refused before any SQL on a database without them."""

from __future__ import annotations

from typing import Any

import pytest

from hare import Connections, fields
from hare.contrib.test import requires_features
from hare.ddl import (
    DatabaseFunction,
    DatabaseSequence,
    FunctionVolatility,
    Grant,
    GrantTarget,
    MaterializedView,
    Policy,
    PolicyCommand,
    Privilege,
    RawSQLTerm,
    RowLevelSecurity,
    View,
)
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.migration import Migration
from hare.migrations.operations import (
    AddFunction,
    AddGrant,
    AddMaterializedView,
    AddPolicy,
    AddSequence,
    AddView,
    AlterFunction,
    AlterMaterializedView,
    AlterPolicy,
    AlterRowLevelSecurity,
    AlterSequence,
    AlterView,
    CreateModel,
    RefreshMaterializedView,
    RemoveField,
    RemoveFunction,
    RemoveGrant,
    RemoveMaterializedView,
    RemovePolicy,
    RemoveSequence,
    RemoveView,
    RenameFunction,
    RenameMaterializedView,
    RenamePolicy,
    RenameSequence,
    RenameView,
)
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.migrations.writer import MigrationWriter
from hare.models.enums import ModelOption
from hare.query.expressions import Q

TABLE = "schema_migration_invoice"
READER_ROLE = "hare_schema_object_reader"
SEQUENCE = DatabaseSequence("schema_migration_number", start=100, increment=2, owned_by="number")
FUNCTION = DatabaseFunction(
    "schema_migration_tenant",
    returns="integer",
    body=RawSQLTerm("SELECT nullif(current_setting('hare.tenant', true), '')::integer"),
    language="sql",
    volatility=FunctionVolatility.STABLE,
)
VIEW = View("schema_migration_paid", query=RawSQLTerm(f"SELECT id, amount FROM {TABLE} WHERE paid"))
MATERIALIZED_VIEW = MaterializedView(
    "schema_migration_totals",
    query=RawSQLTerm(f"SELECT tenant_id, sum(amount) AS total FROM {TABLE} GROUP BY tenant_id"),
    unique_columns=("tenant_id",),
)
POLICY = Policy(
    "schema_migration_tenant_rows",
    command=PolicyCommand.SELECT,
    roles=(READER_ROLE,),
    using=RawSQLTerm("tenant_id = schema_migration_tenant()"),
)
WRITE_POLICY = Policy("schema_migration_paid_rows", with_check=Q(paid=False), permissive=False)
TABLE_GRANT = Grant(privileges=(Privilege.SELECT,), roles=(READER_ROLE,))
COLUMN_GRANT = Grant(privileges=(Privilege.UPDATE,), roles=(READER_ROLE,), columns=("paid",))
VIEW_GRANT = Grant((Privilege.SELECT,), (READER_ROLE,), on=GrantTarget.VIEW, object_name=VIEW.name)
SEQUENCE_GRANT = Grant((Privilege.USAGE,), (READER_ROLE,), on=GrantTarget.SEQUENCE, object_name=SEQUENCE.name)
FUNCTION_GRANT = Grant((Privilege.EXECUTE,), (READER_ROLE,), on=GrantTarget.FUNCTION, object_name=FUNCTION.name)


def get_fields() -> list[tuple[str, Any]]:
    return [
        ("id", fields.IntField(primary_key=True)),
        ("number", fields.BigIntField(null=True)),
        ("tenant_id", fields.IntField()),
        ("amount", fields.IntField()),
        ("paid", fields.BooleanField(default=False)),
    ]


def get_full_options() -> dict[str, Any]:
    return {
        "sequences": [SEQUENCE],
        "functions": [FUNCTION],
        "views": [VIEW],
        "materialized_views": [MATERIALIZED_VIEW],
        "row_level_security": RowLevelSecurity.ENABLED,
        "policies": [POLICY, WRITE_POLICY],
        "grants": [TABLE_GRANT, COLUMN_GRANT, VIEW_GRANT, SEQUENCE_GRANT, FUNCTION_GRANT],
    }


def get_state(model_fields: list[tuple[str, Any]] | None = None, **options: Any) -> State:
    state = State(models={}, apps=StateApps())
    CreateModel(
        name="Invoice", fields=model_fields or get_fields(), options={"table": TABLE, "app": "models", **options}
    ).state_forward("models", state)
    return state


def get_types(operations: list[Any]) -> list[str]:
    return [type(operation).__name__ for operation in operations]


# Declarations


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: View("", query=RawSQLTerm("SELECT 1")), "name must be a non-empty string"),
        (lambda: View("v", query=RawSQLTerm("  ")), "the query can't be empty"),
        (lambda: View("v", query=1), "takes a queryset, a callable returning one or RawSQLTerm"),
        (lambda: View("v", query="SELECT 1"), "takes a queryset, a callable returning one or RawSQLTerm"),
        (lambda: DatabaseFunction("f", returns="integer", body="SELECT 1"), "body takes RawSQLTerm"),
        (lambda: MaterializedView("v", query=RawSQLTerm("SELECT 1"), with_data="yes"), "with_data must be a bool"),
        (
            lambda: MaterializedView("v", query=RawSQLTerm("SELECT 1"), unique_columns="id"),
            "unique_columns must be a sequence",
        ),
        (
            lambda: MaterializedView("v", query=RawSQLTerm("SELECT 1"), unique_columns=("a", "a")),
            "names a column twice",
        ),
        (lambda: DatabaseFunction("f", returns="", body=RawSQLTerm("x")), "returns must be a non-empty string"),
        (
            lambda: DatabaseFunction("f", returns="int", body=RawSQLTerm("x"), arguments="a int"),
            "arguments must be a sequence",
        ),
        (
            lambda: DatabaseFunction("f", returns="int", body=RawSQLTerm("x"), language="sql; drop"),
            "language must be a plain",
        ),
        (
            lambda: DatabaseFunction("f", returns="int", body=RawSQLTerm("x"), volatility="pure"),
            "volatility must be one of",
        ),
        (
            lambda: DatabaseFunction("f", returns="int", body=RawSQLTerm("x"), security_definer=1),
            "security_definer must be a bool",
        ),
        (lambda: DatabaseSequence("s", increment=0), "increment can't be 0"),
        (lambda: DatabaseSequence("s", increment=True), "increment must be an integer"),
        (lambda: DatabaseSequence("s", start=2**63), "start must be within"),
        (lambda: DatabaseSequence("s", cache=0), "cache must be within 1.."),
        (lambda: DatabaseSequence("s", cache=10**7), "cache must be within 1.."),
        (lambda: DatabaseSequence("s", minimum=5, maximum=5), "must be below maximum"),
        (lambda: DatabaseSequence("s", start=1, minimum=5), "must be within minimum..maximum"),
        (lambda: DatabaseSequence("s", cycle="no"), "cycle must be a bool"),
        (lambda: DatabaseSequence("s", owned_by=""), "owned_by must be a field name"),
        (lambda: Policy("p"), "give a using or a with_check condition"),
        (lambda: Policy("p", command="merge", using=Q(paid=True)), "command must be one of"),
        (lambda: Policy("p", command=PolicyCommand.INSERT, using=Q(paid=True)), "takes only with_check"),
        (lambda: Policy("p", command=PolicyCommand.DELETE, with_check=Q(paid=True)), "takes only using"),
        (lambda: Policy("p", using="paid"), "takes a Q over the model's fields or RawSQLTerm"),
        (lambda: Policy("p", using=Q(paid=True), roles="reader"), "roles must be a sequence"),
        (lambda: Policy("p", using=Q(paid=True), permissive=None), "permissive must be a bool"),
        (lambda: Grant(privileges=(), roles=("r",)), "privileges must be a non-empty sequence"),
        (lambda: Grant(privileges=(Privilege.EXECUTE,), roles=("r",)), "has no 'execute' privilege"),
        (lambda: Grant(privileges=(Privilege.SELECT, Privilege.SELECT), roles=("r",)), "names a privilege twice"),
        (lambda: Grant(privileges=(Privilege.SELECT,), roles=()), "roles must be a non-empty sequence"),
        (lambda: Grant((Privilege.SELECT,), ("r",), object_name="v"), "names a view, sequence or function"),
        (lambda: Grant((Privilege.SELECT,), ("r",), on=GrantTarget.VIEW), "needs the object_name"),
        (lambda: Grant((Privilege.SELECT,), ("r",), on="schema", object_name="v"), "Grant.on must be one of"),
        (
            lambda: Grant((Privilege.SELECT,), ("r",), on=GrantTarget.VIEW, object_name="v", columns=("id",)),
            "limits a grant on the table only",
        ),
        (lambda: Grant((Privilege.DELETE,), ("r",), columns=("id",)), "limits only the"),
        (lambda: Grant((Privilege.SELECT,), ("r",), with_grant_option="yes"), "with_grant_option must be a bool"),
    ],
)
def test_a_wrong_declaration_is_refused(make, message):
    with pytest.raises(ConfigurationError, match=message):
        make()


def test_a_declaration_takes_lists_and_strings_for_its_enums():
    grant = Grant(privileges=["select", "update"], roles=["reader"], columns=["paid"])
    assert grant.privileges == (Privilege.SELECT, Privilege.UPDATE)
    assert grant.roles == ("reader",)
    assert grant.columns == ("paid",)
    assert grant.name is None
    assert Policy("p", roles=["reader"], using=Q(paid=True)).roles == ("reader",)
    assert DatabaseFunction("f", returns="int", body=RawSQLTerm("x"), arguments=["a int"]).arguments == ("a int",)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"views": [MATERIALIZED_VIEW]}, "Meta.views entries must be View"),
        ({"views": VIEW}, "Meta.views must be a list"),
        ({"functions": [VIEW]}, "Meta.functions entries must be DatabaseFunction"),
        ({"views": [VIEW, View(VIEW.name, query=RawSQLTerm("SELECT 2"))]}, "declares two objects named"),
        ({"grants": [TABLE_GRANT, TABLE_GRANT]}, "declares Grant"),
        (
            {"views": [View("shared", query=RawSQLTerm("SELECT 1"))], "sequences": [DatabaseSequence("shared")]},
            "named 'shared' twice",
        ),
        ({"row_level_security": "on"}, "Meta.row_level_security must be one of"),
        ({"policies": [POLICY]}, "Meta.policies take effect only with row level security on"),
    ],
)
def test_wrong_meta_options_are_refused(options, message):
    with pytest.raises(ConfigurationError, match=message):
        get_state(**options)


# Autodetection


def test_a_new_model_gets_its_objects_once_every_table_exists():
    operations = OperationGenerator(State(models={}, apps=StateApps()), get_state(**get_full_options())).generate()
    assert get_types(operations) == [
        "CreateModel",
        "AddFunction",
        "AddView",
        "AddMaterializedView",
        "AlterRowLevelSecurity",
        "AddPolicy",
        "AddPolicy",
        "AddGrant",
        "AddGrant",
        "AddGrant",
        "AddGrant",
        "AddGrant",
    ]
    create_model = operations[0]
    # The sequence stays with the table - a column default may take from it.
    assert create_model.options["sequences"] == (SEQUENCE,)
    assert not {"views", "materialized_views", "functions", "policies", "grants", "row_level_security"} & set(
        create_model.options
    )


def test_unchanged_objects_have_no_operations():
    assert OperationGenerator(get_state(**get_full_options()), get_state(**get_full_options())).generate() == []


def test_changes_are_altered_renamed_and_removed_around_the_other_operations():
    old = get_state(**get_full_options())
    renamed_function = DatabaseFunction(
        "schema_migration_tenant_id",
        returns=FUNCTION.returns,
        body=FUNCTION.body,
        language="sql",
        volatility=FunctionVolatility.STABLE,
    )
    new = get_state(
        sequences=[DatabaseSequence(SEQUENCE.name, start=100, increment=3, owned_by="number")],
        functions=[renamed_function],
        views=[View(VIEW.name, query=RawSQLTerm(f"SELECT id FROM {TABLE} WHERE paid"))],
        row_level_security=RowLevelSecurity.FORCED,
        policies=[POLICY],
        grants=[
            TABLE_GRANT,
            COLUMN_GRANT,
            VIEW_GRANT,
            SEQUENCE_GRANT,
            Grant((Privilege.EXECUTE,), (READER_ROLE,), on=GrantTarget.FUNCTION, object_name=renamed_function.name),
        ],
    )
    operations = OperationGenerator(old, new).generate()
    assert get_types(operations) == [
        "RemoveGrant",
        "RemovePolicy",
        "RemoveMaterializedView",
        "RenameFunction",
        "AlterSequence",
        "AlterView",
        "AlterRowLevelSecurity",
        "AddGrant",
    ]
    assert operations[0].grant == FUNCTION_GRANT
    assert operations[1].name == WRITE_POLICY.name
    assert (operations[3].old_name, operations[3].new_name) == (FUNCTION.name, renamed_function.name)
    assert operations[6].row_level_security == RowLevelSecurity.FORCED


def test_objects_reading_a_removed_column_are_dropped_before_it_and_created_after():
    old = get_state(views=[VIEW], policies=[POLICY, WRITE_POLICY], row_level_security=RowLevelSecurity.ENABLED)
    without_amount = [(name, field) for name, field in get_fields() if name not in ("amount", "paid")]
    new = get_state(
        without_amount,
        views=[View(VIEW.name, query=RawSQLTerm(f"SELECT id FROM {TABLE}"))],
        policies=[POLICY, Policy(WRITE_POLICY.name, with_check=Q(tenant_id=1), permissive=False)],
        row_level_security=RowLevelSecurity.ENABLED,
    )
    operations = OperationGenerator(old, new).generate()
    assert get_types(operations) == [
        "RemovePolicy",
        "RemoveView",
        "RemoveField",
        "RemoveField",
        "AddView",
        "AddPolicy",
    ]
    assert all(isinstance(operation, RemoveField) for operation in operations[2:4])


def test_turning_row_level_security_off_and_dropping_functions_and_sequences_last():
    old = get_state(sequences=[SEQUENCE], functions=[FUNCTION], row_level_security=RowLevelSecurity.ENABLED)
    operations = OperationGenerator(old, get_state()).generate()
    assert get_types(operations) == ["AlterRowLevelSecurity", "RemoveFunction", "RemoveSequence"]
    assert operations[0].row_level_security is None


# Writer and state


def get_all_operations() -> list[Any]:
    return [
        AddSequence("Invoice", SEQUENCE),
        AlterSequence("Invoice", DatabaseSequence(SEQUENCE.name, increment=-1, minimum=-50, maximum=0, cycle=True)),
        RenameSequence("Invoice", SEQUENCE.name, "renamed_sequence"),
        RemoveSequence("Invoice", SEQUENCE.name),
        AddFunction("Invoice", FUNCTION),
        AlterFunction(
            "Invoice",
            DatabaseFunction(FUNCTION.name, returns="bigint", body=RawSQLTerm("SELECT 1"), security_definer=True),
        ),
        RenameFunction("Invoice", FUNCTION.name, "renamed_function"),
        RemoveFunction("Invoice", FUNCTION.name),
        AddView("Invoice", VIEW),
        AlterView("Invoice", View(VIEW.name, query=RawSQLTerm("SELECT 2"))),
        RenameView("Invoice", VIEW.name, "renamed_view"),
        RemoveView("Invoice", VIEW.name),
        AddMaterializedView("Invoice", MATERIALIZED_VIEW),
        AlterMaterializedView(
            "Invoice", MaterializedView(MATERIALIZED_VIEW.name, query=RawSQLTerm("SELECT 1"), with_data=False)
        ),
        RenameMaterializedView("Invoice", MATERIALIZED_VIEW.name, "renamed_totals"),
        RemoveMaterializedView("Invoice", MATERIALIZED_VIEW.name),
        RefreshMaterializedView("Invoice", MATERIALIZED_VIEW.name, concurrently=True),
        AlterRowLevelSecurity("Invoice", RowLevelSecurity.FORCED),
        AlterRowLevelSecurity("Invoice", None),
        AddPolicy("Invoice", POLICY),
        AddPolicy("Invoice", WRITE_POLICY),
        AlterPolicy("Invoice", Policy(POLICY.name, roles=("PUBLIC",), using=Q(tenant_id=3) | Q(paid=True))),
        RenamePolicy("Invoice", POLICY.name, "renamed_policy"),
        RemovePolicy("Invoice", POLICY.name),
        AddGrant("Invoice", COLUMN_GRANT),
        AddGrant("Invoice", FUNCTION_GRANT),
        AddGrant("Invoice", Grant((Privilege.ALL,), ("PUBLIC",), with_grant_option=True)),
        RemoveGrant("Invoice", VIEW_GRANT),
    ]


def test_the_operations_are_written_and_read_back():
    operations = get_all_operations()
    source = MigrationWriter("0001_initial", "models", operations).as_string()
    namespace: dict[str, Any] = {}
    exec(compile(source, "<migration>", "exec"), namespace)  # noqa: S102 - the test's own text
    read_back = namespace["Migration"]("0001_initial", "models").operations
    assert [operation.deconstruct() for operation in read_back] == [
        operation.deconstruct() for operation in operations
    ]
    assert [operation.describe() for operation in read_back] == [operation.describe() for operation in operations]


def test_the_operations_change_the_models_state():
    state = get_state()
    for operation in (
        AddSequence("Invoice", SEQUENCE),
        AddFunction("Invoice", FUNCTION),
        AddView("Invoice", VIEW),
        AddMaterializedView("Invoice", MATERIALIZED_VIEW),
        AlterRowLevelSecurity("Invoice", RowLevelSecurity.ENABLED),
        AddPolicy("Invoice", POLICY),
        AddGrant("Invoice", TABLE_GRANT),
    ):
        operation.state_forward("models", state)
    options = state.models[("models", "Invoice")].options
    assert options[ModelOption.SEQUENCES] == (SEQUENCE,)
    assert options[ModelOption.FUNCTIONS] == (FUNCTION,)
    assert options[ModelOption.VIEWS] == (VIEW,)
    assert options[ModelOption.MATERIALIZED_VIEWS] == (MATERIALIZED_VIEW,)
    assert options[ModelOption.ROW_LEVEL_SECURITY] == RowLevelSecurity.ENABLED
    assert options[ModelOption.POLICIES] == (POLICY,)
    assert options[ModelOption.GRANTS] == (TABLE_GRANT,)
    model = state.apps.get_model("models.Invoice")
    assert model._meta.views == (VIEW,)
    assert model._meta.row_level_security == RowLevelSecurity.ENABLED

    new_view = View(VIEW.name, query=RawSQLTerm("SELECT 2"))
    AlterView("Invoice", new_view).state_forward("models", state)
    RenameView("Invoice", VIEW.name, "renamed_view").state_forward("models", state)
    RenameSequence("Invoice", SEQUENCE.name, "renamed_sequence").state_forward("models", state)
    RemovePolicy("Invoice", POLICY.name).state_forward("models", state)
    RemoveGrant("Invoice", Grant(privileges=["select"], roles=[READER_ROLE])).state_forward("models", state)
    AlterRowLevelSecurity("Invoice", None).state_forward("models", state)
    options = state.models[("models", "Invoice")].options
    assert options[ModelOption.VIEWS] == (View("renamed_view", query=RawSQLTerm("SELECT 2")),)
    assert options[ModelOption.SEQUENCES][0].name == "renamed_sequence"
    assert ModelOption.POLICIES not in options
    assert ModelOption.GRANTS not in options
    assert ModelOption.ROW_LEVEL_SECURITY not in options
    with pytest.raises(IncompatibleStateError, match="Grant of select on table"):
        RemoveGrant("Invoice", TABLE_GRANT).state_forward("models", state)
    with pytest.raises(IncompatibleStateError):
        RemoveView("Invoice", "missing").state_forward("models", state)


def test_operations_refuse_wrong_arguments():
    with pytest.raises(ConfigurationError, match="row_level_security must be one of"):
        AlterRowLevelSecurity("Invoice", "on")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="concurrently must be a bool"):
        RefreshMaterializedView("Invoice", "totals", concurrently="yes")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_view_of_a_queryset_is_kept_and_written_as_its_sql(db_simple):
    from tests.testmodels import Tournament

    view = View("tournament_names", query=lambda: Tournament.objects.filter(name="cup").values("id", "name"))
    state = get_state()
    AddView("Invoice", view).state_forward("models", state)
    kept = state.models[("models", "Invoice")].options[ModelOption.VIEWS][0]
    assert isinstance(kept.query, RawSQLTerm)
    assert "'cup'" in kept.query.sql
    assert kept.query.sql == view.get_query_sql()
    assert view.deconstruct()[2]["query"] == kept.query
    # A model declaring it keeps the SQL in its state too, and the autodetector compares that SQL.
    declared = get_state(views=[view])
    assert declared.models[("models", "Invoice")].options[ModelOption.VIEWS] == (kept,)
    assert OperationGenerator(state, declared).generate() == []
    queryset_view = View("tournament_names", query=Tournament.objects.filter(name="cup").values("id", "name"))
    assert queryset_view.with_sql_query() == kept
    with pytest.raises(ConfigurationError, match="must return a queryset"):
        View("v", query=lambda: 1).get_query_sql()


# Databases


def get_editor(collect_sql: bool = False):
    connection = Connections.get("models")
    return connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=collect_sql)


def make_migration(name: str, *operations: Any) -> Migration:
    return Migration(name=name, app_label="models", operations=list(operations))


def get_create_migration() -> Migration:
    return make_migration(
        "0001_initial",
        CreateModel(name="Invoice", fields=get_fields(), options={"table": TABLE, "sequences": [SEQUENCE]}),
        AddFunction("Invoice", FUNCTION),
        AddView("Invoice", VIEW),
        AddMaterializedView("Invoice", MATERIALIZED_VIEW),
        AlterRowLevelSecurity("Invoice", RowLevelSecurity.ENABLED),
        AddPolicy("Invoice", POLICY),
        AddPolicy("Invoice", WRITE_POLICY),
        AddGrant("Invoice", TABLE_GRANT),
        AddGrant("Invoice", COLUMN_GRANT),
        AddGrant("Invoice", VIEW_GRANT),
        AddGrant("Invoice", SEQUENCE_GRANT),
        AddGrant("Invoice", FUNCTION_GRANT),
    )


class Catalog:
    """What the PostgreSQL catalog holds of the test's objects."""

    def __init__(self) -> None:
        self.connection = Connections.get("models")

    async def get_names(self, sql: str) -> list[str]:
        return [row["name"] for row in await self.connection.execute_dicts(sql)]

    async def get_views(self) -> list[str]:
        return await self.get_names("SELECT viewname AS name FROM pg_views WHERE viewname LIKE 'schema_migration%'")

    async def get_view_definition(self, name: str) -> str:
        rows = await self.connection.execute_dicts(f"SELECT definition FROM pg_views WHERE viewname = '{name}'")
        return str(rows[0]["definition"])

    async def get_materialized_views(self) -> list[str]:
        return await self.get_names(
            "SELECT matviewname AS name FROM pg_matviews WHERE matviewname LIKE 'schema_migration%' ORDER BY 1"
        )

    async def get_functions(self) -> list[str]:
        return await self.get_names(
            "SELECT proname || '(' || pg_get_function_identity_arguments(oid) || ') ' || "
            "pg_get_function_result(oid) AS name FROM pg_proc WHERE proname LIKE 'schema_migration%' ORDER BY 1"
        )

    async def get_sequences(self) -> list[str]:
        return await self.get_names(
            "SELECT sequencename || ' ' || increment_by || ' ' || coalesce(start_value, 0) AS name "
            "FROM pg_sequences WHERE sequencename LIKE 'schema_migration%' "
            "AND sequencename NOT LIKE '%_id_seq' ORDER BY 1"
        )

    async def get_row_level_security(self) -> tuple[bool, bool]:
        rows = await self.connection.execute_dicts(
            f"SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = '{TABLE}'"
        )
        return rows[0]["relrowsecurity"], rows[0]["relforcerowsecurity"]

    async def get_policies(self) -> list[str]:
        return await self.get_names(
            "SELECT policyname || ' ' || permissive || ' ' || cmd || ' ' || array_to_string(roles, ',') AS name "
            f"FROM pg_policies WHERE tablename = '{TABLE}' ORDER BY 1"
        )

    async def get_privileges(self) -> dict[str, bool]:
        rows = await self.connection.execute_dicts(
            f"SELECT has_table_privilege('{READER_ROLE}', '{TABLE}', 'SELECT') AS table_select, "
            f"has_column_privilege('{READER_ROLE}', '{TABLE}', 'paid', 'UPDATE') AS paid_update, "
            f"has_column_privilege('{READER_ROLE}', '{TABLE}', 'amount', 'UPDATE') AS amount_update, "
            f"has_table_privilege('{READER_ROLE}', '{VIEW.name}', 'SELECT') AS view_select, "
            f"has_sequence_privilege('{READER_ROLE}', '{SEQUENCE.name}', 'USAGE') AS sequence_usage"
        )
        return dict(rows[0])

    async def create_role(self) -> None:
        await self.connection.execute_script(
            f"DO $role$ BEGIN CREATE ROLE {READER_ROLE}; "
            "EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL; END $role$;"
        )

    async def drop_everything(self) -> None:
        await self.connection.execute_script(
            "DROP MATERIALIZED VIEW IF EXISTS schema_migration_totals, schema_migration_renamed_totals; "
            "DROP VIEW IF EXISTS schema_migration_paid, schema_migration_renamed_paid; "
            f"DROP TABLE IF EXISTS {TABLE} CASCADE; "
            "DROP FUNCTION IF EXISTS schema_migration_tenant(); DROP FUNCTION IF EXISTS schema_migration_renamed(); "
            "DROP FUNCTION IF EXISTS schema_migration_plain(); "
            "DROP SEQUENCE IF EXISTS schema_migration_number, schema_migration_renamed_number;"
        )


@requires_features(supports_views=True)
@pytest.mark.asyncio
async def test_every_operation_is_applied_and_unapplied_on_postgresql(db_simple):
    catalog = Catalog()
    await catalog.drop_everything()
    await catalog.create_role()
    empty = State(models={}, apps=StateApps())
    try:
        initial = get_create_migration()
        state = await initial.apply(empty.clone(), schema_editor=get_editor())
        assert await catalog.get_views() == [VIEW.name]
        assert await catalog.get_materialized_views() == [MATERIALIZED_VIEW.name]
        assert await catalog.get_functions() == ["schema_migration_tenant() integer"]
        assert await catalog.get_sequences() == ["schema_migration_number 2 100"]
        assert await catalog.get_row_level_security() == (True, False)
        assert await catalog.get_policies() == [
            "schema_migration_paid_rows RESTRICTIVE ALL public",
            f"schema_migration_tenant_rows PERMISSIVE SELECT {READER_ROLE}",
        ]
        assert await catalog.get_privileges() == {
            "table_select": True,
            "paid_update": True,
            "amount_update": False,
            "view_select": True,
            "sequence_usage": True,
        }
        await catalog.connection.execute_script(
            f"INSERT INTO {TABLE} (id, tenant_id, amount, paid) VALUES (1, 1, 5, false), (2, 2, 7, false)"
        )

        # Changed in place, renamed and refreshed - and back.
        changed = make_migration(
            "0002_changed",
            AlterView(
                "Invoice", View(VIEW.name, query=RawSQLTerm(f"SELECT id, amount, tenant_id FROM {TABLE} WHERE paid"))
            ),
            AlterMaterializedView(
                "Invoice",
                MaterializedView(
                    MATERIALIZED_VIEW.name,
                    query=RawSQLTerm(f"SELECT tenant_id, count(*) AS invoices FROM {TABLE} GROUP BY tenant_id"),
                    unique_columns=("tenant_id",),
                ),
            ),
            RefreshMaterializedView("Invoice", MATERIALIZED_VIEW.name, concurrently=True),
            AlterFunction(
                "Invoice",
                DatabaseFunction(FUNCTION.name, returns="integer", body=RawSQLTerm("SELECT 1"), language="sql"),
            ),
            AlterSequence("Invoice", DatabaseSequence(SEQUENCE.name, increment=10, owned_by=None)),
            AlterRowLevelSecurity("Invoice", RowLevelSecurity.FORCED),
            AlterPolicy("Invoice", Policy(POLICY.name, command=PolicyCommand.SELECT, using=Q(tenant_id=1))),
            AlterPolicy(
                "Invoice",
                Policy(WRITE_POLICY.name, command=PolicyCommand.UPDATE, using=Q(paid=False), permissive=False),
            ),
            RemoveGrant("Invoice", VIEW_GRANT),
            RenameView("Invoice", VIEW.name, "schema_migration_renamed_paid"),
            RenameMaterializedView("Invoice", MATERIALIZED_VIEW.name, "schema_migration_renamed_totals"),
            RemoveGrant("Invoice", FUNCTION_GRANT),
            RenameFunction("Invoice", FUNCTION.name, "schema_migration_renamed"),
            RemoveGrant("Invoice", SEQUENCE_GRANT),
            RenameSequence("Invoice", SEQUENCE.name, "schema_migration_renamed_number"),
            RenamePolicy("Invoice", POLICY.name, "schema_migration_renamed_rows"),
        )
        before_changed = state.clone()
        state = await changed.apply(state, schema_editor=get_editor())
        assert await catalog.get_views() == ["schema_migration_renamed_paid"]
        assert "tenant_id" in await catalog.get_view_definition("schema_migration_renamed_paid")
        assert await catalog.get_materialized_views() == ["schema_migration_renamed_totals"]
        rows = await catalog.connection.execute_dicts(
            "SELECT tenant_id, invoices FROM schema_migration_renamed_totals ORDER BY 1"
        )
        assert [(row["tenant_id"], row["invoices"]) for row in rows] == [(1, 1), (2, 1)]
        indexes = await catalog.get_names(
            "SELECT indexname AS name FROM pg_indexes WHERE tablename = 'schema_migration_renamed_totals'"
        )
        assert indexes == ["schema_migration_renamed_totals_unique"]
        assert await catalog.get_functions() == ["schema_migration_renamed() integer"]
        assert await catalog.get_sequences() == ["schema_migration_renamed_number 10 1"]
        assert await catalog.get_row_level_security() == (True, True)
        assert await catalog.get_policies() == [
            "schema_migration_paid_rows RESTRICTIVE UPDATE public",
            "schema_migration_renamed_rows PERMISSIVE SELECT public",
        ]
        await changed.unapply(before_changed, schema_editor=get_editor())
        assert await catalog.get_views() == [VIEW.name]
        assert "tenant_id" not in await catalog.get_view_definition(VIEW.name)
        assert await catalog.get_materialized_views() == [MATERIALIZED_VIEW.name]
        assert await catalog.get_functions() == ["schema_migration_tenant() integer"]
        assert await catalog.get_sequences() == ["schema_migration_number 2 100"]
        assert await catalog.get_row_level_security() == (True, False)
        assert await catalog.get_policies() == [
            "schema_migration_paid_rows RESTRICTIVE ALL public",
            f"schema_migration_tenant_rows PERMISSIVE SELECT {READER_ROLE}",
        ]
        assert (await catalog.get_privileges())["view_select"] is True
        state = before_changed

        # A new result type replaces the function and grants on it again.
        plain_function = DatabaseFunction(
            "schema_migration_plain", returns="integer", body=RawSQLTerm("SELECT 1"), language="sql"
        )
        plain = make_migration(
            "0003_plain",
            AddFunction("Invoice", plain_function),
            AddGrant(
                "Invoice",
                Grant((Privilege.EXECUTE,), (READER_ROLE,), on=GrantTarget.FUNCTION, object_name=plain_function.name),
            ),
        )
        before_plain = state.clone()
        state = await plain.apply(state, schema_editor=get_editor())
        retyped = make_migration(
            "0004_retyped",
            AlterFunction(
                "Invoice",
                DatabaseFunction(plain_function.name, returns="bigint", body=RawSQLTerm("SELECT 2"), language="sql"),
            ),
        )
        before_retyped = state.clone()
        state = await retyped.apply(state, schema_editor=get_editor())
        assert await catalog.get_functions() == [
            "schema_migration_plain() bigint",
            "schema_migration_tenant() integer",
        ]
        rows = await catalog.connection.execute_dicts(
            f"SELECT has_function_privilege('{READER_ROLE}', 'schema_migration_plain()', 'EXECUTE') AS execute, "
            "(SELECT proacl::text FROM pg_proc WHERE proname = 'schema_migration_plain') AS acl"
        )
        assert rows[0]["execute"] is True
        assert READER_ROLE in rows[0]["acl"]
        await retyped.unapply(before_retyped, schema_editor=get_editor())
        assert await catalog.get_functions() == [
            "schema_migration_plain() integer",
            "schema_migration_tenant() integer",
        ]
        await plain.unapply(before_plain, schema_editor=get_editor())
        assert await catalog.get_functions() == ["schema_migration_tenant() integer"]
        state = before_plain

        # Removed - and back.
        removed = make_migration(
            "0004_removed",
            RemoveGrant("Invoice", COLUMN_GRANT),
            RemoveGrant("Invoice", VIEW_GRANT),
            RemoveGrant("Invoice", SEQUENCE_GRANT),
            RemoveGrant("Invoice", FUNCTION_GRANT),
            RemovePolicy("Invoice", POLICY.name),
            RemovePolicy("Invoice", WRITE_POLICY.name),
            AlterRowLevelSecurity("Invoice", None),
            RemoveMaterializedView("Invoice", MATERIALIZED_VIEW.name),
            RemoveView("Invoice", VIEW.name),
            RemoveFunction("Invoice", FUNCTION.name),
            RemoveSequence("Invoice", SEQUENCE.name),
        )
        before_removed = state.clone()
        state = await removed.apply(state, schema_editor=get_editor())
        assert await catalog.get_views() == []
        assert await catalog.get_materialized_views() == []
        assert await catalog.get_functions() == []
        assert await catalog.get_sequences() == []
        assert await catalog.get_row_level_security() == (False, False)
        assert await catalog.get_policies() == []
        rows = await catalog.connection.execute_dicts(
            f"SELECT has_column_privilege('{READER_ROLE}', '{TABLE}', 'paid', 'UPDATE') AS paid_update"
        )
        assert rows[0]["paid_update"] is False
        await removed.unapply(before_removed, schema_editor=get_editor())
        assert await catalog.get_views() == [VIEW.name]
        assert await catalog.get_materialized_views() == [MATERIALIZED_VIEW.name]
        assert await catalog.get_sequences() == ["schema_migration_number 2 100"]
        assert len(await catalog.get_policies()) == 2
        assert (await catalog.get_privileges())["view_select"] is True

        await initial.unapply(empty, schema_editor=get_editor())
        assert await catalog.get_views() == []
        assert await catalog.get_materialized_views() == []
        assert await catalog.get_functions() == []
        assert await catalog.get_sequences() == []
    finally:
        await catalog.drop_everything()


@requires_features(supports_views=True)
@pytest.mark.asyncio
async def test_a_deleted_model_takes_its_objects_with_it(db_simple):
    catalog = Catalog()
    await catalog.drop_everything()
    await catalog.create_role()
    try:
        state = await make_migration(
            "0001_initial",
            CreateModel(name="Invoice", fields=get_fields(), options={"table": TABLE, **get_full_options()}),
        ).apply(State(models={}, apps=StateApps()), schema_editor=get_editor())
        assert await catalog.get_views() == [VIEW.name]
        assert await catalog.get_policies() != []
        from hare.migrations.operations import DeleteModel

        await make_migration("0002_deleted", DeleteModel(name="Invoice")).apply(state, schema_editor=get_editor())
        assert await catalog.get_views() == []
        assert await catalog.get_materialized_views() == []
        assert await catalog.get_functions() == []
        assert await catalog.get_sequences() == []
    finally:
        await catalog.drop_everything()


@requires_features(supports_views=True)
@pytest.mark.asyncio
async def test_the_autodetected_operations_run_and_drop_the_readers_of_a_removed_column(db_simple):
    catalog = Catalog()
    await catalog.drop_everything()
    await catalog.create_role()
    empty = State(models={}, apps=StateApps())
    try:
        full = get_state(**get_full_options())
        initial = make_migration("0001_initial", *OperationGenerator(empty, full).generate())
        state = await initial.apply(empty.clone(), schema_editor=get_editor())
        assert await catalog.get_views() == [VIEW.name]
        assert len(await catalog.get_policies()) == 2
        without_paid = [(name, field) for name, field in get_fields() if name != "paid"]
        options = get_full_options()
        options["views"] = [View(VIEW.name, query=RawSQLTerm(f"SELECT id, amount FROM {TABLE}"))]
        options["policies"] = [POLICY]
        options["grants"] = [TABLE_GRANT, VIEW_GRANT, SEQUENCE_GRANT, FUNCTION_GRANT]
        changed = make_migration(
            "0002_without_paid", *OperationGenerator(full, get_state(without_paid, **options)).generate()
        )
        assert get_types(changed.operations) == [
            "RemoveGrant",
            "RemovePolicy",
            "RemoveView",
            "RemoveField",
            "AddView",
        ]
        before_changed = state.clone()
        state = await changed.apply(state, schema_editor=get_editor())
        assert "paid" not in await catalog.get_view_definition(VIEW.name)
        rows = await catalog.connection.execute_dicts(
            f"SELECT has_table_privilege('{READER_ROLE}', '{VIEW.name}', 'SELECT') AS view_select"
        )
        assert rows[0]["view_select"] is True
        assert len(await catalog.get_policies()) == 1
        await changed.unapply(before_changed, schema_editor=get_editor())
        assert "paid" in await catalog.get_view_definition(VIEW.name)
        assert len(await catalog.get_policies()) == 2
        await initial.unapply(empty, schema_editor=get_editor())
        assert await catalog.get_views() == []
    finally:
        await catalog.drop_everything()


@requires_features(supports_views=True)
@pytest.mark.asyncio
async def test_a_rebuilt_table_gets_its_objects_back(db_simple):
    catalog = Catalog()
    await catalog.drop_everything()
    await catalog.create_role()
    try:
        state = await make_migration(
            "0001_initial",
            CreateModel(name="Invoice", fields=get_fields(), options={"table": TABLE, **get_full_options()}),
        ).apply(State(models={}, apps=StateApps()), schema_editor=get_editor())
        await catalog.connection.execute_script(
            f"INSERT INTO {TABLE} (id, tenant_id, amount, paid) VALUES (1, 1, 5, true)"
        )
        await get_editor().table_rebuild.remake_table(state.apps.get_model("models.Invoice"))
        assert await catalog.get_views() == [VIEW.name]
        assert await catalog.get_materialized_views() == [MATERIALIZED_VIEW.name]
        assert await catalog.get_sequences() == ["schema_migration_number 2 100"]
        assert await catalog.get_row_level_security() == (True, False)
        assert len(await catalog.get_policies()) == 2
        assert await catalog.get_privileges() == {
            "table_select": True,
            "paid_update": True,
            "amount_update": False,
            "view_select": True,
            "sequence_usage": True,
        }
        rows = await catalog.connection.execute_dicts(f"SELECT id, amount FROM {VIEW.name}")
        assert [(row["id"], row["amount"]) for row in rows] == [(1, 5)]
        owners = await catalog.get_names(
            "SELECT attribute.attname AS name FROM pg_depend "
            "JOIN pg_class sequence ON sequence.oid = pg_depend.objid "
            "JOIN pg_attribute attribute ON attribute.attrelid = pg_depend.refobjid "
            "AND attribute.attnum = pg_depend.refobjsubid "
            f"WHERE sequence.relname = '{SEQUENCE.name}' AND pg_depend.deptype = 'a'"
        )
        assert owners == ["number"]
    finally:
        await catalog.drop_everything()


@requires_features(supports_views=True)
@pytest.mark.asyncio
async def test_sqlmigrate_shows_the_sql(db_simple):
    editor = get_editor(collect_sql=True)
    state = await get_create_migration().apply(
        State(models={}, apps=StateApps()), schema_editor=editor, collect_sql=True
    )
    await make_migration(
        "0002_refresh", RefreshMaterializedView("Invoice", MATERIALIZED_VIEW.name, concurrently=True)
    ).apply(state, schema_editor=editor, collect_sql=True)
    sql = "\n".join(editor.collected_sql)
    for expected in (
        'CREATE SEQUENCE "schema_migration_number" INCREMENT BY 2 START WITH 100 CACHE 1 NO CYCLE;',
        f'ALTER SEQUENCE "schema_migration_number" OWNED BY "{TABLE}"."number";',
        'CREATE FUNCTION "schema_migration_tenant"() RETURNS integer\nLANGUAGE sql STABLE',
        'CREATE VIEW "schema_migration_paid" AS\nSELECT id, amount',
        'CREATE MATERIALIZED VIEW "schema_migration_totals" AS',
        'CREATE UNIQUE INDEX "schema_migration_totals_unique" ON "schema_migration_totals" ("tenant_id");',
        f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY;',
        f'CREATE POLICY "schema_migration_tenant_rows" ON "{TABLE}" AS PERMISSIVE FOR SELECT TO "{READER_ROLE}" '
        "USING (tenant_id = schema_migration_tenant());",
        'CREATE POLICY "schema_migration_paid_rows"',
        f'GRANT SELECT ON TABLE "{TABLE}" TO "{READER_ROLE}";',
        f'GRANT UPDATE ("paid") ON TABLE "{TABLE}" TO "{READER_ROLE}";',
        f'GRANT EXECUTE ON FUNCTION "schema_migration_tenant"() TO "{READER_ROLE}";',
        'REFRESH MATERIALIZED VIEW CONCURRENTLY "schema_migration_totals";',
    ):
        assert expected in sql


@requires_features(supports_views=True)
@pytest.mark.asyncio
async def test_postgresql_refuses_what_it_cant_do_before_any_sql(db_simple):
    editor = get_editor(collect_sql=True)
    state = await make_migration("0001_initial", CreateModel(name="Invoice", fields=get_fields())).apply(
        State(models={}, apps=StateApps()), schema_editor=editor, collect_sql=True
    )
    editor.collected_sql.clear()
    view_without_index = MaterializedView("schema_migration_plain", query=RawSQLTerm("SELECT 1 AS one"))
    with pytest.raises(ConfigurationError, match="without unique_columns"):
        await make_migration(
            "0002_refresh",
            AddMaterializedView("Invoice", view_without_index),
            RefreshMaterializedView("Invoice", view_without_index.name, concurrently=True),
        ).apply(state.clone(), schema_editor=editor, collect_sql=True)
    with pytest.raises(ConfigurationError, match="declares no view named 'missing'"):
        await make_migration(
            "0003_grant", AddGrant("Invoice", Grant((Privilege.SELECT,), ("r",), on="view", object_name="missing"))
        ).apply(state.clone(), schema_editor=editor, collect_sql=True)
    with pytest.raises(ConfigurationError, match="names 'missing', which is no column field"):
        await make_migration(
            "0004_sequence", AddSequence("Invoice", DatabaseSequence("schema_migration_s", owned_by="missing"))
        ).apply(state.clone(), schema_editor=editor, collect_sql=True)
    with pytest.raises(ConfigurationError, match="the body can't hold"):
        await make_migration(
            "0005_function",
            AddFunction("Invoice", DatabaseFunction("f", returns="int", body=RawSQLTerm("SELECT $hare_function$"))),
        ).apply(state.clone(), schema_editor=editor, collect_sql=True)


@requires_features(supports_views=False)
@pytest.mark.asyncio
async def test_a_database_without_them_refuses_every_operation_before_any_sql(db_simple):
    state = get_state(**get_full_options())
    plain = get_state()
    connection = Connections.get("models")
    operations_and_states = [
        (AddSequence("Invoice", SEQUENCE), plain),
        (AlterSequence("Invoice", SEQUENCE), state),
        (RenameSequence("Invoice", SEQUENCE.name, "other"), state),
        (RemoveSequence("Invoice", SEQUENCE.name), state),
        (AddFunction("Invoice", FUNCTION), plain),
        (AlterFunction("Invoice", FUNCTION), state),
        (RenameFunction("Invoice", FUNCTION.name, "other"), state),
        (RemoveFunction("Invoice", FUNCTION.name), state),
        (AddView("Invoice", VIEW), plain),
        (AlterView("Invoice", VIEW), state),
        (RenameView("Invoice", VIEW.name, "other"), state),
        (RemoveView("Invoice", VIEW.name), state),
        (AddMaterializedView("Invoice", MATERIALIZED_VIEW), plain),
        (AlterMaterializedView("Invoice", MATERIALIZED_VIEW), state),
        (RenameMaterializedView("Invoice", MATERIALIZED_VIEW.name, "other"), state),
        (RemoveMaterializedView("Invoice", MATERIALIZED_VIEW.name), state),
        (RefreshMaterializedView("Invoice", MATERIALIZED_VIEW.name), state),
        (AlterRowLevelSecurity("Invoice", RowLevelSecurity.FORCED), state),
        (AddPolicy("Invoice", POLICY), get_state(row_level_security=RowLevelSecurity.ENABLED)),
        (AlterPolicy("Invoice", POLICY), state),
        (RenamePolicy("Invoice", POLICY.name, "other"), state),
        (RemovePolicy("Invoice", POLICY.name), state),
        (AddGrant("Invoice", TABLE_GRANT), plain),
        (RemoveGrant("Invoice", TABLE_GRANT), state),
    ]
    for operation, before in operations_and_states:
        for collect_sql in (False, True):
            editor = get_editor(collect_sql=collect_sql)
            after = before.clone()
            operation.state_forward("models", after)
            with pytest.raises(UnSupportedError, match="not supported on"):
                await operation.database_forward("models", before, after, editor)
            if not isinstance(operation, RefreshMaterializedView):
                with pytest.raises(UnSupportedError, match="not supported on"):
                    await operation.database_backward("models", after, before, editor)
            assert editor.collected_sql == []
    # A new model declaring them isn't created at all.
    editor = get_editor()
    with pytest.raises(UnSupportedError, match="not supported on"):
        await make_migration(
            "0001_initial", CreateModel(name="Invoice", fields=get_fields(), options={"table": TABLE, "views": [VIEW]})
        ).apply(State(models={}, apps=StateApps()), schema_editor=editor)
    tables = await connection.execute_dicts(f"SELECT name FROM sqlite_master WHERE name = '{TABLE}'")
    assert tables == []
