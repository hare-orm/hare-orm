from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import QueryError
from hare.models.write.returned_values import ReturnedValues
from hare.models.write.rules.written_instances import WrittenInstances

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
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
        written_field_names: Sequence[str] = (),
        applied_columns: Collection[str] | None = None,
    ) -> None:
        """Sets each object's database-computed columns from its ``RETURNING`` row - by position; where
        the database returns rows in no order, by the conflict target's values when there is one, else
        by primary key; by the conflict target's values when rows were skipped.

        Args:
            bulk_create: The bulk insert.
            objects_item: The objects of one statement, in order.
            returned_rows: Its returned rows.
            conflict_field_names: The ``on_conflict=[...]`` fields, when rows can be skipped.
            written_field_names: The fields the statement wrote - an ``ignore_conflicts`` row a database
                returns in no order is matched by all of them, a conflict target with a NULL telling no row
                apart.
            applied_columns: The columns of a row set on its object - every one when None; the columns
                returned only to match a row are left out.

        Raises:
            QueryError: Rows were skipped and there is no conflict target to match the rest by.
        """
        types = bulk_create.dialect.types
        if len(objects_item) == 1 and len(returned_rows) == 1:
            ReturnedRowsMatching.apply_returning_row(bulk_create, objects_item[0], returned_rows[0], applied_columns)
            return
        matches: Iterable[tuple[TModel, Any]]
        if bulk_create._ignore_conflicts and written_field_names:
            # Of objects sharing a conflict key the first is written; the values compared as stored - a field
            # may write a value otherwise (an email's domain in lower case).
            if bulk_create.dialect.features.guarantees_returning_order:
                matches = ReturnedValues.match_rows_in_order(
                    bulk_create.model,
                    types,
                    objects_item,
                    returned_rows,
                    list(conflict_field_names) or bulk_create.model._meta.primary_key_attribute_names,
                )
            else:
                matches = ReturnedValues.match_rows_by_stored_values(
                    bulk_create.model, types, objects_item, returned_rows, written_field_names
                )
            for obj, row in matches:
                ReturnedRowsMatching.apply_returning_row(bulk_create, obj, row, applied_columns)
            return
        if len(returned_rows) != len(objects_item) and not conflict_field_names and bulk_create._ignore_conflicts:
            # Without on_conflict every object carries its key - checked before the insert.
            conflict_field_names = bulk_create.model._meta.primary_key_attribute_names
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
            # By the conflict target where there is one - a conflicting object's row holds the key of
            # the row it updated, not the one the object was given.
            matches = ReturnedValues.match_rows(
                bulk_create.model,
                types,
                objects_item,
                returned_rows,
                list(bulk_create._on_conflict or ()) or bulk_create.model._meta.primary_key_attribute_names,
            )
        else:
            matches = zip(objects_item, returned_rows, strict=True)
        for obj, row in matches:
            ReturnedRowsMatching.apply_returning_row(bulk_create, obj, row)

    @staticmethod
    def apply_returning_row(
        bulk_create: BulkCreateQuery[Any], obj: TModel, row: Any, columns: Collection[str] | None = None
    ) -> None:
        """Marks ``obj`` saved and sets every field ``row`` carries a column for.

        Args:
            bulk_create: The bulk insert.
            obj: The object whose row was written.
            row: Its RETURNING row, keyed by DB column.
            columns: Only these columns of the row - every one when None.
        """
        custom_generated_pk = obj._custom_generated_pk
        if bulk_create._inserted_objects is not None:
            bulk_create._inserted_objects.append(obj)
        WrittenInstances.mark_saved((obj,), bulk_create._connection)
        ReturnedValues.apply_row(
            bulk_create.model, bulk_create.dialect.types, obj, row, columns, connection=bulk_create._connection
        )
        # Assigning the pk on a saved instance clears the flag - a caller-supplied pk stays marked so.
        object.__setattr__(obj, "_custom_generated_pk", custom_generated_pk)
