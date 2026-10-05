"""What a model declares beside its table on PostgreSQL, created by generate_schemas(): a view of a
queryset, a materialized view refreshed at runtime (concurrently too), a function, a sequence owned by
a column and read at runtime, row level security with a policy filtering a role's rows, and grants."""

from __future__ import annotations

import pytest

from hare.exceptions import QueryError
from hare.transactions.transactions import Transactions
from tests.dialects.postgresql.models_schema_objects import READER_ROLE, SchemaObjectInvoice


async def add_invoices() -> None:
    await SchemaObjectInvoice.objects.create(id=1, tenant_id=1, amount=10, paid=True)
    await SchemaObjectInvoice.objects.create(id=2, tenant_id=1, amount=5)
    await SchemaObjectInvoice.objects.create(id=3, tenant_id=2, amount=7, paid=True)


@pytest.mark.asyncio
async def test_the_view_selects_the_querysets_rows(db_schema_objects):
    await add_invoices()
    rows = await db_schema_objects.get_connection().execute_dicts(
        "SELECT id, amount FROM schema_object_paid_invoices ORDER BY id"
    )
    assert [(row["id"], row["amount"]) for row in rows] == [(1, 10), (3, 7)]


@pytest.mark.asyncio
async def test_the_materialized_view_is_refreshed_at_runtime(db_schema_objects):
    connection = db_schema_objects.get_connection()
    assert await connection.execute_dicts("SELECT * FROM schema_object_invoice_totals") == []
    await add_invoices()
    await SchemaObjectInvoice.objects.refresh_materialized_view("schema_object_invoice_totals")
    rows = await connection.execute_dicts("SELECT tenant_id, total FROM schema_object_invoice_totals ORDER BY 1")
    assert [(row["tenant_id"], row["total"]) for row in rows] == [(1, 15), (2, 7)]
    await SchemaObjectInvoice.objects.create(id=4, tenant_id=2, amount=1)
    await SchemaObjectInvoice.objects.refresh_materialized_view("schema_object_invoice_totals", concurrently=True)
    rows = await connection.execute_dicts("SELECT tenant_id, total FROM schema_object_invoice_totals ORDER BY 1")
    assert [(row["tenant_id"], row["total"]) for row in rows] == [(1, 15), (2, 8)]
    indexes = await connection.execute_dicts(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'schema_object_invoice_totals'"
    )
    assert [row["indexname"] for row in indexes] == ["schema_object_invoice_totals_unique"]


@pytest.mark.asyncio
async def test_refreshing_an_undeclared_view_or_with_a_wrong_argument_is_refused(db_schema_objects):
    with pytest.raises(QueryError, match="declares no materialized view named 'missing'"):
        await SchemaObjectInvoice.objects.refresh_materialized_view("missing")
    with pytest.raises(QueryError, match="takes a bool"):
        await SchemaObjectInvoice.objects.refresh_materialized_view(
            "schema_object_invoice_totals",
            concurrently="yes",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_the_sequence_hands_out_numbers_and_is_owned_by_its_column(db_schema_objects):
    first = await SchemaObjectInvoice.objects.get_next_sequence_value("schema_object_invoice_number")
    second = await SchemaObjectInvoice.objects.get_next_sequence_value("schema_object_invoice_number")
    assert second - first == 5
    assert first >= 1000
    rows = await db_schema_objects.get_connection().execute_dicts(
        "SELECT attribute.attname AS column_name FROM pg_depend "
        "JOIN pg_class sequence ON sequence.oid = pg_depend.objid "
        "JOIN pg_attribute attribute ON attribute.attrelid = pg_depend.refobjid "
        "AND attribute.attnum = pg_depend.refobjsubid "
        "WHERE sequence.relname = 'schema_object_invoice_number' AND pg_depend.deptype = 'a'"
    )
    assert [row["column_name"] for row in rows] == ["number"]
    with pytest.raises(QueryError, match="declares no sequence named 'missing'"):
        await SchemaObjectInvoice.objects.get_next_sequence_value("missing")


@pytest.mark.asyncio
async def test_the_function_is_called_from_sql(db_schema_objects):
    connection = db_schema_objects.get_connection()
    async with Transactions.atomic("models"):
        await connection.execute_script("SET LOCAL hare.tenant = '7'")
        rows = await connection.execute_dicts("SELECT schema_object_current_tenant() AS tenant")
    assert rows[0]["tenant"] == 7
    rows = await connection.execute_dicts(
        "SELECT provolatile::text AS provolatile FROM pg_proc WHERE proname = 'schema_object_current_tenant'"
    )
    assert rows[0]["provolatile"] == "s"


@pytest.mark.asyncio
async def test_row_level_security_and_the_policy_filter_the_readers_rows(db_schema_objects):
    await add_invoices()
    connection = db_schema_objects.get_connection()
    rows = await connection.execute_dicts(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'schema_object_invoice'"
    )
    assert (rows[0]["relrowsecurity"], rows[0]["relforcerowsecurity"]) == (True, False)
    async with Transactions.atomic("models"):
        await connection.execute_script(f"SET LOCAL ROLE {READER_ROLE}; SET LOCAL hare.tenant = '1'")
        rows = await connection.execute_dicts("SELECT id FROM schema_object_invoice ORDER BY id")
        await connection.execute_script("RESET ROLE")
    assert [row["id"] for row in rows] == [1, 2]


@pytest.mark.asyncio
async def test_the_grants_give_the_role_its_privileges(db_schema_objects):
    rows = await db_schema_objects.get_connection().execute_dicts(
        f"SELECT has_table_privilege('{READER_ROLE}', 'schema_object_invoice', 'SELECT') AS table_select, "
        f"has_table_privilege('{READER_ROLE}', 'schema_object_invoice', 'INSERT') AS table_insert, "
        f"has_table_privilege('{READER_ROLE}', 'schema_object_paid_invoices', 'SELECT') AS view_select, "
        f"has_sequence_privilege('{READER_ROLE}', 'schema_object_invoice_number', 'USAGE') AS sequence_usage"
    )
    assert rows[0] == {"table_select": True, "table_insert": False, "view_select": True, "sequence_usage": True}


@pytest.mark.asyncio
async def test_drift_reads_the_views_back(db_schema_objects):
    """PostgreSQL's views were taken as declared - a view dropped, defined another way, or a
    materialized view without its unique index was no drift."""
    from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
    from hare.migrations.drift import detect_drift

    connection = db_schema_objects.get_connection()
    state = MigrationAutodetector(
        db_schema_objects.apps, {"models": {"models": [], "default_connection": "models"}}
    ).current_state()

    async def get_operation_types() -> list[str]:
        result = await detect_drift(connection, state, ["models"])
        return sorted(type(operation).__name__ for operation in result.operations)

    assert await get_operation_types() == []
    [row] = await connection.execute_dicts(
        "SELECT pg_get_viewdef('schema_object_paid_invoices'::regclass) AS definition"
    )
    await connection.execute_script(
        "CREATE OR REPLACE VIEW schema_object_paid_invoices AS "
        "SELECT id, amount FROM schema_object_invoice WHERE paid AND amount > 0"
    )
    assert await get_operation_types() == ["AlterView"]
    await connection.execute_script(
        f"CREATE OR REPLACE VIEW schema_object_paid_invoices AS {row['definition'].rstrip().rstrip(';')}"
    )
    assert await get_operation_types() == []

    [index_row] = await connection.execute_dicts(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'schema_object_invoice_totals'"
    )
    await connection.execute_script(f'DROP INDEX "{index_row["indexname"]}"')
    assert await get_operation_types() == ["AlterMaterializedView"]
    await connection.execute_script(index_row["indexdef"])
    assert await get_operation_types() == []


@pytest.mark.asyncio
async def test_drift_reads_the_functions_sequences_policies_grants_and_row_level_security_back(db_schema_objects):
    """Only the views and the dictionaries of ClickHouse were read back - a function, a sequence, a
    policy, a grant or row level security changed by hand was no drift."""
    from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
    from hare.migrations.drift import detect_drift

    connection = db_schema_objects.get_connection()
    state = MigrationAutodetector(
        db_schema_objects.apps, {"models": {"models": [], "default_connection": "models"}}
    ).current_state()

    async def get_operation_types() -> list[str]:
        result = await detect_drift(connection, state, ["models"])
        return sorted(type(operation).__name__ for operation in result.operations)

    assert await get_operation_types() == []
    [function_row] = await connection.execute_dicts(
        "SELECT pg_get_functiondef('schema_object_current_tenant'::regproc) AS definition"
    )
    for changed_sql, restored_sql, expected in (
        (
            "CREATE OR REPLACE FUNCTION schema_object_current_tenant() RETURNS integer LANGUAGE sql STABLE "
            "AS $$ SELECT 7 $$",
            function_row["definition"],
            ["AlterFunction"],
        ),
        (
            "ALTER SEQUENCE schema_object_invoice_number INCREMENT BY 10",
            "ALTER SEQUENCE schema_object_invoice_number INCREMENT BY 5",
            ["AlterSequence"],
        ),
        (
            "ALTER TABLE schema_object_invoice FORCE ROW LEVEL SECURITY",
            "ALTER TABLE schema_object_invoice NO FORCE ROW LEVEL SECURITY",
            ["AlterRowLevelSecurity"],
        ),
        (
            "ALTER POLICY schema_object_tenant_rows ON schema_object_invoice TO PUBLIC",
            f"ALTER POLICY schema_object_tenant_rows ON schema_object_invoice TO {READER_ROLE}",
            ["AlterPolicy"],
        ),
        (
            f"REVOKE USAGE ON SEQUENCE schema_object_invoice_number FROM {READER_ROLE}",
            f"GRANT USAGE ON SEQUENCE schema_object_invoice_number TO {READER_ROLE}",
            ["AddGrant"],
        ),
    ):
        await connection.execute_script(changed_sql)
        assert await get_operation_types() == expected, changed_sql
        await connection.execute_script(restored_sql)
        assert await get_operation_types() == [], restored_sql
