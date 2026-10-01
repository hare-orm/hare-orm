from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.int_field import IntField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.fields.base.field import Field
    from hare.models import Model


class WrittenValueCheck:
    """The fields an UPDATE sets from an expression whose written value is checked in Python - read
    back through ``RETURNING`` in a transaction. A value the field rejects rolls the UPDATE back.

    Args:
        model: The updated model.
    """

    __slots__ = ("model", "converted_columns", "validated_columns")

    def __init__(self, model: type[Model]) -> None:
        self.model = model
        #: (column, field) of an integer or decimal field on a database whose column types don't
        #: enforce its range and scale - converted for the write like a plain value.
        self.converted_columns: list[tuple[str, Field[Any]]] = []
        #: (column, field) of any other field with validators its column type doesn't enforce.
        self.validated_columns: list[tuple[str, Field[Any]]] = []

    def __bool__(self) -> bool:
        return bool(self.converted_columns or self.validated_columns)

    def add(self, dialect: Dialect, column: str, field: Field[Any]) -> None:
        """Takes a field set from an expression - checked when its written value can break a
        rule of the field the database doesn't enforce.

        Args:
            dialect: The database's dialect.
            column: The field's column.
            field: The field.
        """
        if not dialect.enforces_numeric_ranges and isinstance(field, (IntField, DecimalField)):
            self.converted_columns.append((column, field))
        elif field.get_validators_not_enforced_by_column(dialect):
            self.validated_columns.append((column, field))

    def get_primary_key_columns(self) -> list[str]:
        """The primary key columns - a rewrite finds the row by them."""
        meta = self.model._meta
        return [meta.fields_db_projection[name] for name in meta.pk_attr_names]

    def get_returning_columns(self) -> list[str]:
        """The columns the UPDATE returns for the check - the primary key, then every checked
        column."""
        return [
            *self.get_primary_key_columns(),
            *(column for column, _field in self.converted_columns),
            *(column for column, _field in self.validated_columns),
        ]

    async def execute(self, db: DatabaseClient, sql: str, params: list[Any]) -> tuple[int, Sequence[Any]]:
        """Runs the UPDATE and checks every value it wrote, in one transaction.

        Args:
            db: The connection.
            sql: The UPDATE, returning ``get_returning_columns()``.
            params: Its parameters.

        Returns:
            The number of updated rows and the returned rows.

        Raises:
            ValidationError: A written value fails the field's validation.
        """
        table = self.model._meta.basetable
        primary_key_columns = self.get_primary_key_columns()
        async with db._in_transaction() as transaction_db:
            types = transaction_db.dialect.types
            rows_affected, returned_rows = await transaction_db.execute(sql, params)
            for raw_row in returned_rows:
                row = dict(raw_row)
                corrections: dict[str, Any] = {}
                for column, field in self.converted_columns:
                    raw_value = row[column]
                    if raw_value is None:
                        continue
                    # Raises for a value out of the field's range.
                    corrected_value = types.get_db_value(field, raw_value, None)
                    # Compared as text: an arithmetic result stored as `3.0` is rewritten as
                    # `3.0000`, the text a plain write stores.
                    if isinstance(field, DecimalField) and raw_value != format(corrected_value, "f"):
                        corrections[column] = corrected_value
                for column, field in self.validated_columns:
                    raw_value = row[column]
                    if raw_value is None:
                        continue
                    # Validated in its database form, the check a plain value gets - a
                    # CharEnumField stores a non-str enum value as its str().
                    types.get_db_value(field, types.get_python_value(field, raw_value), None)
                if corrections:
                    correction_query = transaction_db.query_class.update(table)
                    for column, corrected_value in corrections.items():
                        correction_query = correction_query.set(column, corrected_value)
                    for primary_key_column in primary_key_columns:
                        correction_query = correction_query.where(table[primary_key_column] == row[primary_key_column])
                    await transaction_db.execute(*correction_query.get_parameterized_sql())
        return rows_affected, returned_rows
