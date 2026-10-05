from __future__ import annotations

from hare import Model, fields
from hare.ddl import (
    DatabaseFunction,
    DatabaseSequence,
    FunctionVolatility,
    Grant,
    GrantTarget,
    MaterializedView,
    Policy,
    Privilege,
    RawSQLTerm,
    RowLevelSecurity,
    View,
)

#: The role the grants and the policy are for - created by the fixture.
READER_ROLE = "hare_schema_object_reader"


class SchemaObjectInvoice(Model):
    """A table with one object of every type a model declares beside it."""

    id = fields.IntField(primary_key=True)
    number = fields.BigIntField(null=True)
    tenant_id = fields.IntField()
    amount = fields.IntField()
    paid = fields.BooleanField(default=False)

    class Meta:
        table = "schema_object_invoice"
        sequences = [
            DatabaseSequence("schema_object_invoice_number", start=1000, increment=5, owned_by="number"),
        ]
        functions = [
            DatabaseFunction(
                "schema_object_current_tenant",
                returns="integer",
                body=RawSQLTerm("SELECT nullif(current_setting('hare.tenant', true), '')::integer"),
                language="sql",
                volatility=FunctionVolatility.STABLE,
            ),
        ]
        views = [
            View(
                "schema_object_paid_invoices",
                query=lambda: SchemaObjectInvoice.objects.filter(paid=True).values("id", "amount"),
            ),
        ]
        materialized_views = [
            MaterializedView(
                "schema_object_invoice_totals",
                query=RawSQLTerm(
                    "SELECT tenant_id, sum(amount) AS total FROM schema_object_invoice GROUP BY tenant_id"
                ),
                unique_columns=("tenant_id",),
            ),
        ]
        row_level_security = RowLevelSecurity.ENABLED
        policies = [
            Policy(
                "schema_object_tenant_rows",
                command="select",
                roles=(READER_ROLE,),
                using=RawSQLTerm("tenant_id = schema_object_current_tenant()"),
            ),
        ]
        grants = [
            Grant(privileges=(Privilege.SELECT,), roles=(READER_ROLE,)),
            Grant(
                privileges=(Privilege.SELECT,),
                roles=(READER_ROLE,),
                on=GrantTarget.VIEW,
                object_name="schema_object_paid_invoices",
            ),
            Grant(
                privileges=(Privilege.USAGE,),
                roles=(READER_ROLE,),
                on=GrantTarget.SEQUENCE,
                object_name="schema_object_invoice_number",
            ),
        ]
