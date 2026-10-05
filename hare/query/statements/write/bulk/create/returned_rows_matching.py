from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import QueryError
from hare.models.instances.instance_connections import InstanceConnections
from hare.models.write.returned_values import ReturnedValues

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class ReturnedRowsMatching:
    """The rows a RETURNING insert reads back, matched to their objects - by position, or by primary
    key where the database returns them in no particular order - and the columns they carry."""

    @staticmethod
    def get_returning_columns(bulk_create: BulkCreateQuery[Any], omit_fields: Collection[str] = ()) -> list[str]:
        """The columns a ``returning=True`` INSERT returns: the primary key, the generated
        columns, the fields left to their database default and, for an upsert, the version an
        update bumps.

        Args:
            bulk_create: The bulk insert.
            omit_fields: The fields left out of the insert.
        """
        return bulk_create._get_insert_statement().get_returning_columns(
            primary_key_given=False,
            omitted_field_names=omit_fields,
            returns_primary_key=True,
            returns_version=bool(bulk_create._update_fields),
        )

    @staticmethod
    def get_returned_field_names(bulk_create: BulkCreateQuery[Any], omit_fields: set[str]) -> list[str]:
        """Field names a RETURNING row of this call can set on an object.

        Args:
            bulk_create: The bulk insert.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.

        Returns:
            The field names, deduplicated.
        """
        meta = bulk_create.model._meta
        returned_columns = ReturnedRowsMatching.get_returning_columns(bulk_create, omit_fields)
        if bulk_create._ignore_conflicts and bulk_create._on_conflict:
            returned_columns += [meta.fields_db_projection[name] for name in bulk_create._on_conflict]
        return list(dict.fromkeys(meta.fields_db_projection_reverse[column] for column in returned_columns))

    @staticmethod
    def objects_have_primary_keys(bulk_create: BulkCreateQuery[Any]) -> bool:
        """Whether every object carries its primary key - a returned row is then matched to its
        object by key rather than by position.

        Args:
            bulk_create: The bulk insert.

        Returns:
            True when every key is set.
        """
        if not bulk_create.model._meta.has_primary_key:
            return False
        bulk_create._objects = list(bulk_create._objects)
        if bulk_create.model._meta.has_composite_primary_key:
            return all(None not in obj.pk for obj in bulk_create._objects)
        return all(obj.pk is not None for obj in bulk_create._objects)

    @staticmethod
    def populate_returned_fields_from_returning_rows(
        bulk_create: BulkCreateQuery[Any],
        objects_item: list[TModel],
        returned_rows: Sequence[Any],
        conflict_field_names: Sequence[str] = (),
    ) -> None:
        """Sets each object's database-computed columns from its ``RETURNING`` row - by position, by
        primary key where the database returns rows in no order, or by the conflict target's values
        when rows were skipped.

        Args:
            bulk_create: The bulk insert.
            objects_item: The objects of one statement, in order.
            returned_rows: Its returned rows.
            conflict_field_names: The ``on_conflict=[...]`` fields, when rows can be skipped.

        Raises:
            QueryError: Rows were skipped and there is no conflict target to match the rest by.
        """
        types = bulk_create.dialect.types
        matches: Iterable[tuple[TModel, Any]]
        if len(returned_rows) != len(objects_item):
            if not conflict_field_names:
                raise QueryError(
                    f"bulk_create() on {bulk_create.model.__name__}: {len(objects_item) - len(returned_rows)} "
                    f"of {len(objects_item)} object(s) were skipped by ON CONFLICT DO NOTHING, and "
                    "returning=True cannot match the surviving rows back to their source objects "
                    "without an explicit on_conflict=[...] column list to match by value."
                )
            # Several objects sharing a conflict key: the first is inserted, the rest conflict
            # with it - the first owns the row.
            matches = ReturnedValues.match_rows(
                bulk_create.model, types, objects_item, returned_rows, conflict_field_names
            )
        elif not bulk_create.dialect.features.guarantees_returning_order:
            matches = ReturnedValues.match_rows(
                bulk_create.model,
                types,
                objects_item,
                returned_rows,
                bulk_create.model._meta.primary_key_attribute_names,
            )
        else:
            matches = zip(objects_item, returned_rows, strict=True)
        for obj, row in matches:
            ReturnedRowsMatching.apply_returning_row(bulk_create, obj, row)

    @classmethod
    async def read_back_rows(cls, bulk_create: BulkCreateQuery[Any], omit_fields: set[str]) -> None:
        """Sets on each written object the values the database gave its row - read by the objects'
        keys after the insert, on a database returning no rows.

        Args:
            bulk_create: The bulk insert.
            omit_fields: Fields left out of the INSERT - left to their database default.
        """
        meta = bulk_create.model._meta
        columns = cls.get_returning_columns(bulk_create, omit_fields)
        objects_by_key = {obj.pk: obj for obj in bulk_create._objects if obj.pk is not None}
        if not columns or not objects_by_key:
            return
        key_names = list(meta.primary_key_attribute_names)
        returned_names = [
            name
            for name in dict.fromkeys(meta.fields_db_projection_reverse[column] for column in columns)
            if name not in key_names
        ]
        if not returned_names:
            return
        # Local import: the queryset package imports the query statements.
        from hare.query.queryset.queryset import QuerySet

        queryset: QuerySet[Any] = QuerySet(bulk_create.model).filter(pk__in=list(objects_by_key))
        queryset._apply_connection(bulk_create._connection)
        queryset._connection_explicitly_chosen = True
        for values in await queryset.values_list(*key_names, *returned_names):
            key = values[0] if len(key_names) == 1 else tuple(values[: len(key_names)])
            obj = objects_by_key.get(key)
            if obj is not None:
                for field_name, value in zip(returned_names, values[len(key_names) :], strict=True):
                    setattr(obj, field_name, value)

    @staticmethod
    def apply_returning_row(bulk_create: BulkCreateQuery[Any], obj: TModel, row: Any) -> None:
        """Marks ``obj`` saved and sets every field ``row`` carries a column for.

        Args:
            bulk_create: The bulk insert.
            obj: The object whose row was written.
            row: Its RETURNING row, keyed by DB column.
        """
        custom_generated_pk = obj._custom_generated_pk
        object.__setattr__(obj, "_saved_in_db", True)
        InstanceConnections.remember_connection(obj, bulk_create._connection)
        ReturnedValues.apply_row(bulk_create.model, bulk_create.dialect.types, obj, row)
        # Assigning the pk on a saved instance clears the flag - a caller-supplied pk stays marked so.
        object.__setattr__(obj, "_custom_generated_pk", custom_generated_pk)
