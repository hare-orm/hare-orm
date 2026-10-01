from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING, Any

from hare.fields.generated import GeneratedField
from hare.sql.terms.base.parameter import Parameter

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.models.write.insert_conflict import InsertConflict
    from hare.sql.queries.builder.query_builder import QueryBuilder


class InsertStatement:
    """The INSERT of one model's rows on one connection - the fields written, the rows,
    ``ON CONFLICT`` and ``RETURNING``. ``save()`` and ``bulk_create()`` build every INSERT here.

    Args:
        model: The model.
        db: The connection.
    """

    __slots__ = ("model", "db")

    def __init__(self, model: type[Model], db: DatabaseClient) -> None:
        self.model = model
        self.db = db

    def get_field_names(self, *, with_primary_key: bool, omitted_field_names: Collection[str] = ()) -> list[str]:
        """The fields an INSERT writes: every field the database doesn't generate.

        Args:
            with_primary_key: The rows carry a primary key the caller gave - written though the
                database would generate it.
            omitted_field_names: Fields left to their database default.

        Returns:
            The field names, in model field order.
        """
        meta = self.model._meta
        field_names = []
        for field_name in meta.fields_db_projection:
            field_object = meta.fields_map[field_name]
            # A GeneratedField's column is never written. A FK's shadow column copied from one
            # resets .generated, so the flag decides - not the class alone.
            if isinstance(field_object, GeneratedField) and field_object.generated:
                continue
            if field_name in omitted_field_names:
                continue
            # Of the columns the database assigns, only the primary key can be given.
            if not field_object.generated or (with_primary_key and field_object.pk):
                field_names.append(field_name)
        return field_names

    def get_columns(self, field_names: Sequence[str]) -> list[str]:
        """The columns of fields."""
        fields_db_projection = self.model._meta.fields_db_projection
        return [fields_db_projection[field_name] for field_name in field_names]

    def get_returning_columns(
        self,
        *,
        primary_key_given: bool,
        omitted_field_names: Collection[str] = (),
        returns_primary_key: bool = False,
        returns_version: bool = False,
    ) -> list[str]:
        """The columns an INSERT returns - the values the database gave the row.

        Args:
            primary_key_given: The rows carry their primary key - a generated one isn't returned.
            omitted_field_names: Fields left to their database default - returned.
            returns_primary_key: Return the primary key first, whatever gave it.
            returns_version: Return ``Meta.optimistic_lock_field`` - an upsert bumps it.

        Returns:
            The columns, without repeats.
        """
        meta = self.model._meta
        primary_key_columns = self.get_columns(meta.pk_attr_names)
        columns = list(primary_key_columns) if returns_primary_key else []
        columns.extend(
            column
            for column in meta.generated_db_fields
            if not (primary_key_given and not returns_primary_key and column in primary_key_columns)
        )
        columns.extend(self.get_columns(list(omitted_field_names)))
        if returns_version and meta.optimistic_lock_field:
            columns.append(meta.fields_db_projection[meta.optimistic_lock_field])
        return list(dict.fromkeys(columns))

    @staticmethod
    def get_parameter_row(column_count: int) -> list[Parameter]:
        """One row of placeholders, numbered from the first."""
        return [Parameter(idx=position + 1) for position in range(column_count)]

    def get_query(
        self,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        conflict: InsertConflict | None = None,
        returning: Sequence[Any] = (),
    ) -> QueryBuilder:
        """The INSERT.

        Args:
            columns: The written columns - none for a row of defaults (``DEFAULT VALUES``).
            rows: The rows - placeholders, values or rendered SQL terms.
            conflict: What a conflicting row does.
            returning: Columns or terms returned, where the database returns rows.

        Returns:
            The query.
        """
        into_table = self.db.query_class.into(self.model._meta.basetable)
        # .columns().insert() of no column renders an empty string, not an INSERT.
        query = into_table.columns(*columns).insert(*rows) if columns else into_table.default_values()
        if conflict is not None:
            query = conflict.apply(self.model, self.db.dialect.types, query)
        if returning and self.db.features.supports_returning:
            query = query.returning(*returning)
        return query
