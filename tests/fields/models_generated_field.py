"""
Testing Models for GeneratedField - dialect-agnostic (STORED) DB-generated columns.
"""

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.fields.db_defaults import SqlDefault
from hare.fields.generated_field import GeneratedField
from hare.models import Model


class PricedItem(Model):
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    quantity = fields.IntField()
    total = GeneratedField(
        expression=RawSQLTerm("price * quantity"),
        output_field=fields.DecimalField(max_digits=12, decimal_places=2),
    )


class LabeledItem(Model):
    quantity = fields.IntField()
    label = GeneratedField(
        expression={
            "sqlite": RawSQLTerm("'Item #' || CAST(quantity AS TEXT)"),
            "columnar": RawSQLTerm("'Item #' || CAST(quantity AS TEXT)"),
            "postgresql": RawSQLTerm("'Item #' || quantity::text"),
        },
        output_field=fields.TextField(),
    )


class TimestampedItem(Model):
    """A GeneratedField(output_field=DatetimeField()) column - `__year`/`__month`/`__hour`/etc
    date-part lookups against it must apply the same timezone-aware extraction a plain
    DatetimeField column already gets."""

    created_at = fields.DatetimeField()
    logged_at = GeneratedField(expression=RawSQLTerm("created_at"), output_field=fields.DatetimeField())


class VersionedPricedItem(Model):
    """PricedItem + Meta.optimistic_lock_field - for bulk_update(returning=True)'s
    interaction with optimistic-locking stale detection specifically."""

    price = fields.DecimalField(max_digits=10, decimal_places=2)
    quantity = fields.IntField()
    version = fields.IntField(default=0)
    total = GeneratedField(
        expression=RawSQLTerm("price * quantity"),
        output_field=fields.DecimalField(max_digits=12, decimal_places=2),
    )

    class Meta:
        optimistic_lock_field = "version"


class AutoReturningPricedItem(Model):
    """PricedItem + Meta.returning = True - for bulk_create()/bulk_update()'s own
    returning=None -> Meta.returning fallback."""

    price = fields.DecimalField(max_digits=10, decimal_places=2)
    quantity = fields.IntField()
    total = GeneratedField(
        expression=RawSQLTerm("price * quantity"),
        output_field=fields.DecimalField(max_digits=12, decimal_places=2),
    )

    class Meta:
        returning = True


class GeneratedFieldFkTargetWarehouse(Model):
    """FK to_field= target below is a GeneratedField - for
    GeneratedFieldFkTargetStockItem's own shadow column INSERT."""

    prefix = fields.CharField(max_length=10)
    number = fields.IntField()
    code = GeneratedField(
        expression={
            "sqlite": RawSQLTerm("prefix || '-' || CAST(number AS TEXT)"),
            "columnar": RawSQLTerm("prefix || '-' || CAST(number AS TEXT)"),
            "postgresql": RawSQLTerm("prefix || '-' || number::text"),
        },
        output_field=fields.CharField(max_length=30),
        unique=True,
    )


class AveragedQuantityItem(Model):
    """quantity_doubled = GeneratedField(output_field=IntField()) - regression coverage for
    Avg() unwrapping a GeneratedField to its output_field before deciding whether the result
    needs FloatField coercion (see Avg._coerce_output_field)."""

    quantity = fields.IntField()
    quantity_doubled = GeneratedField(expression=RawSQLTerm("quantity * 2"), output_field=fields.IntField())


class GeneratedFieldFkTargetStockItem(Model):
    """to_field="code" targets a GeneratedField (unique, not the pk) on
    GeneratedFieldFkTargetWarehouse - the shadow FK column itself is an ordinary, writable
    column (a copy of the target field's type/constraints, not itself DB-generated)."""

    warehouse: fields.ForeignKeyNullableRelation[GeneratedFieldFkTargetWarehouse] = fields.ForeignKeyField(
        "models.GeneratedFieldFkTargetWarehouse",
        to_field="code",
        null=True,
        on_delete=fields.SET_NULL,
        related_name="stock_items",
    )
    name = fields.TextField()


class DbDefaultDoubledItem(Model):
    """A GeneratedField computed from a db_default column."""

    quantity = fields.IntField(db_default=SqlDefault("4"))
    doubled = GeneratedField(expression=RawSQLTerm("quantity * 2"), output_field=fields.IntField())
