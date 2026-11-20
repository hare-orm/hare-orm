from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from itertools import chain
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import QueryError
from hare.models.write.returned_values import ReturnedValues
from hare.models.write.rules.written_instances import WrittenInstances

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.models.write.bound_rows import BoundRows
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
        meta: ModelBinding = bulk_create._binding  # type: ignore[assignment]
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
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        if not model_binding.has_primary_key:
            return False
        if model_binding.has_composite_primary_key:
            return all(None not in obj.pk for obj in bulk_create._objects)
        return all(obj.pk is not None for obj in bulk_create._objects)

    @staticmethod
    def populate_returned_fields_from_returning_rows(
        bulk_create: BulkCreateQuery[Any],
        objects_item: Sequence[TModel],
        returned_rows: Sequence[Any],
        conflict_field_names: Sequence[str] = (),
        written_field_names: Sequence[str] = (),
        applied_columns: Collection[str] | None = None,
        *,
        null_conflict_keys: Sequence[bool] = (),
        bound_rows: BoundRows | None = None,
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
            null_conflict_keys: Bound rows with a distinct-NULL conflict target, in object order.
            bound_rows: The actual converted input rows; matching never converts their source again.

        Raises:
            QueryError: Rows were skipped and there is no conflict target to match the rest by.
        """
        if not returned_rows:
            return
        dialect = bulk_create.dialect
        types = dialect.types
        if len(objects_item) == 1 and len(returned_rows) == 1:
            ReturnedRowsMatching.apply_returning_row(bulk_create, objects_item[0], returned_rows[0], applied_columns)
            return
        matches: Iterable[tuple[TModel, Any]]
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        if bulk_create._ignore_conflicts and written_field_names:
            # Of objects sharing a conflict key the first is written; the values compared as stored - a field
            # may write a value otherwise (an email's domain in lower case).
            if dialect.features.guarantees_returning_order:
                matches = ReturnedValues.match_rows_in_order(
                    model_binding,
                    types,
                    objects_item,
                    returned_rows,
                    list(conflict_field_names) or model_binding.primary_key_attribute_names,
                    bound_rows=bound_rows,
                )
            else:
                matches = ReturnedValues.match_rows_by_stored_values(
                    model_binding,
                    types,
                    objects_item,
                    returned_rows,
                    written_field_names,
                    bound_rows=bound_rows,
                )
            ReturnedRowsMatching.apply_returning_rows(bulk_create, matches, applied_columns)
            return
        if len(returned_rows) != len(objects_item) and not conflict_field_names and bulk_create._ignore_conflicts:
            # Without on_conflict every object carries its key - checked before the insert.
            conflict_field_names = model_binding.primary_key_attribute_names
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
            matches = ReturnedRowsMatching.match_returned_rows(
                bulk_create, objects_item, returned_rows, conflict_field_names, null_conflict_keys, bound_rows
            )
        elif not dialect.features.guarantees_returning_order:
            # By the conflict target where there is one - a conflicting object's row holds the key of
            # the row it updated, not the one the object was given.
            matches = ReturnedRowsMatching.match_returned_rows(
                bulk_create,
                objects_item,
                returned_rows,
                list(bulk_create._on_conflict or ()) or model_binding.primary_key_attribute_names,
                null_conflict_keys,
                bound_rows,
            )
        else:
            matches = zip(objects_item, returned_rows, strict=True)
        ReturnedRowsMatching.apply_returning_rows(bulk_create, matches)

    @staticmethod
    def match_returned_rows(
        bulk_create: BulkCreateQuery[Any],
        objects: Sequence[TModel],
        rows: Sequence[Any],
        field_names: Sequence[str],
        null_conflict_keys: Sequence[bool],
        bound_rows: BoundRows | None,
    ) -> Iterable[tuple[TModel, Any]]:
        """Match distinct-NULL inserts by their known primary keys, other rows by the target.

        The input partition comes from the bound rows, never another field conversion.
        Unknown database-default targets already have their own single-object statements.
        """
        types = bulk_create.dialect.types
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        if not any(null_conflict_keys):
            return ReturnedValues.match_rows(model_binding, types, objects, rows, field_names, bound_rows=bound_rows)
        meta: ModelBinding = model_binding
        columns = [meta.fields_db_projection[name] for name in field_names]
        null_objects = [obj for obj, is_null in zip(objects, null_conflict_keys, strict=True) if is_null]
        other_objects = [obj for obj, is_null in zip(objects, null_conflict_keys, strict=True) if not is_null]
        null_rows = [row for row in rows if any(row[column] is None for column in columns)]
        other_rows = [row for row in rows if all(row[column] is not None for column in columns)]
        return chain(
            ReturnedValues.match_rows(
                model_binding,
                types,
                null_objects,
                null_rows,
                meta.primary_key_attribute_names,
                bound_rows=(
                    bound_rows.select([index for index, is_null in enumerate(null_conflict_keys) if is_null])
                    if bound_rows is not None
                    else None
                ),
            ),
            ReturnedValues.match_rows(
                model_binding,
                types,
                other_objects,
                other_rows,
                field_names,
                bound_rows=(
                    bound_rows.select([index for index, is_null in enumerate(null_conflict_keys) if not is_null])
                    if bound_rows is not None
                    else None
                ),
            ),
        )

    @staticmethod
    def apply_returning_rows(
        bulk_create: BulkCreateQuery[Any],
        matches: Iterable[tuple[TModel, Any]],
        columns: Collection[str] | None = None,
    ) -> None:
        """Share projection readers while preserving lazy matching and per-object publication."""
        readers = None
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        for obj, row in matches:
            if readers is None:
                readers = ReturnedValues.get_returning_readers(
                    model_binding, bulk_create.dialect.types, row.keys() if columns is None else columns
                )
            ReturnedRowsMatching.apply_returning_row(bulk_create, obj, row, columns, readers=readers)

    @staticmethod
    def apply_returning_row(
        bulk_create: BulkCreateQuery[Any],
        obj: TModel,
        row: Any,
        columns: Collection[str] | None = None,
        *,
        readers: Sequence[tuple[str, str, Callable[[Any], Any]]] | None = None,
    ) -> None:
        """Marks ``obj`` saved and sets every field ``row`` carries a column for.

        Args:
            bulk_create: The bulk insert.
            obj: The object whose row was written.
            row: Its RETURNING row, keyed by DB column.
            columns: Only these columns of the row - every one when None.
            readers: Prepared column readers, when already built for this returning projection.
        """
        custom_generated_pk = obj._custom_generated_pk
        if bulk_create._inserted_objects is not None:
            bulk_create._inserted_objects.append(obj)
        WrittenInstances.mark_saved((obj,), bulk_create._connection)
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        ReturnedValues.apply_row(
            model_binding,
            bulk_create.dialect.types,
            obj,
            row,
            columns,
            connection=bulk_create._connection,
            readers=readers,
        )
        # Assigning the pk on a saved instance clears the flag - a caller-supplied pk stays marked so.
        object.__setattr__(obj, "_custom_generated_pk", custom_generated_pk)
