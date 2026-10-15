from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.query.constants import RETURNING_OLD_COLUMN_ALIAS_PREFIX
from hare.query.statements.constants import RETURNING_OLD_VALUES_KEY
from hare.sql.terms.values.old_row_value import OldRowValue

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.field import Field
    from hare.models import Model
    from hare.query.scopes.row_visibility import RowVisibility


class ReturnedRows:
    """What a write's ``returning()`` gives back for each row it wrote - a dict of the named fields,
    or the model instance when no field is named.

    Args:
        model: The written model.
        field_names: The fields - a concrete field, a forward relation (its key, a tuple for a
            composite one) or ``pk``; none for the model instances.
        old_field_names: The fields returned as they were before the write too, under ``"old"`` -
            needs ``field_names``.

    Raises:
        FieldError: A name is given twice, or isn't a concrete field, a forward relation or ``pk``;
            ``old_field_names`` without ``field_names``, or ``"old"`` among ``field_names`` with them.
    """

    __slots__ = ("model", "field_names", "source_names_by_name", "old_source_names_by_name")

    def __init__(self, model: type[Model], field_names: tuple[str, ...], old_field_names: Sequence[str] = ()) -> None:
        if isinstance(old_field_names, str):
            raise FieldError(
                f"returning(old=...) takes a list of field names, got the string {old_field_names!r} - wrap a "
                "single name in a list"
            )
        old_field_names = tuple(old_field_names)
        self.model = model
        self.field_names = field_names
        #: The concrete fields each name reads - one, or the key fields of a composite one.
        self.source_names_by_name: dict[str, tuple[str, ...]] = {}
        for name in field_names:
            if name in self.source_names_by_name:
                raise FieldError(f"returning() names {name!r} twice")
            self.source_names_by_name[name] = self.get_source_names(model, name)
        #: The same, of the fields returned as they were before the write.
        self.old_source_names_by_name: dict[str, tuple[str, ...]] = {}
        if old_field_names and not field_names:
            raise FieldError("returning(old=...) returns dicts - name the fields returned as written too")
        if old_field_names and RETURNING_OLD_VALUES_KEY in self.source_names_by_name:
            raise FieldError(
                f"returning(old=...) returns the old values under {RETURNING_OLD_VALUES_KEY!r} - a field of that "
                "name can't be returned with them"
            )
        for name in old_field_names:
            if name in self.old_source_names_by_name:
                raise FieldError(f"returning(old=...) names {name!r} twice")
            self.old_source_names_by_name[name] = self.get_source_names(model, name)

    @property
    def plan_key(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """What the returned rows hold - part of a plan's key."""
        return self.field_names, tuple(self.old_source_names_by_name)

    def get_call_signature_type_part(self, type_part: tuple[Any, list[Any]]) -> tuple[Any, list[Any]]:
        """The part of a write's call-signature key these rows add.

        Args:
            type_part: The write's own structure and values.

        Returns:
            The structure and values - ``(None, [])`` for a write keeping no plan by its calls.
        """
        structure, values = type_part
        if structure is None:
            return None, []
        return (structure, ("returning", self.plan_key)), values

    def get_old_value_terms(self) -> list[OldRowValue]:
        """The values of the columns before the write the ``RETURNING`` returns.

        Returns:
            A term per column, each once.
        """
        meta = self.model._meta
        columns = dict.fromkeys(
            meta.fields_db_projection[source_name]
            for source_names in self.old_source_names_by_name.values()
            for source_name in source_names
        )
        return [OldRowValue(column, alias=f"{RETURNING_OLD_COLUMN_ALIAS_PREFIX}{column}") for column in columns]

    @staticmethod
    def get_source_names(model: type[Model], name: Any) -> tuple[str, ...]:
        """The concrete fields a returned name reads.

        Args:
            model: The written model.
            name: The name.

        Returns:
            The field names, in key order.

        Raises:
            FieldError: The name isn't a concrete field, a forward relation or ``pk``.
        """
        meta = model._meta
        if name == "pk":
            return meta.primary_key_attribute_names
        field = meta.fields_map.get(name) if isinstance(name, str) else None
        if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
            return tuple(field.source_fields)
        if isinstance(name, str) and name in meta.fields_db_projection:
            return (name,)
        raise FieldError(
            f"returning() takes the {model.__name__} fields written in its table - a field, a forward "
            f"relation or 'pk' - got {name!r}"
        )

    @property
    def returns_instances(self) -> bool:
        """Whether the write returns its model instances - no field is named."""
        return not self.field_names

    def get_returning_columns(self) -> list[str]:
        """The columns the write's ``RETURNING`` returns - the named fields', or the primary key's for
        the instances.

        Returns:
            The columns, each once.
        """
        meta = self.model._meta
        source_names = (
            meta.primary_key_attribute_names
            if self.returns_instances
            else [source_name for names in self.source_names_by_name.values() for source_name in names]
        )
        return list(dict.fromkeys(meta.fields_db_projection[source_name] for source_name in source_names))

    def get_rows(self, types: TypeRegistry, raw_rows: Sequence[Any]) -> list[dict[str, Any]]:
        """The named fields of ``RETURNING`` rows.

        Args:
            types: The connection's type registry.
            raw_rows: The rows, by column name.

        Returns:
            A dict per row, by returned name.
        """
        meta = self.model._meta
        fields_map = meta.fields_map
        projection = meta.fields_db_projection
        rows: list[dict[str, Any]] = []
        for raw_row in raw_rows:
            # A sqlite3.Row has no .items()/.get() - read as a dict like a PostgreSQL row.
            row = dict(raw_row)
            returned_row = {
                name: self.get_value(
                    [
                        types.get_python_value(fields_map[source_name], row[projection[source_name]])
                        for source_name in source_names
                    ]
                )
                for name, source_names in self.source_names_by_name.items()
            }
            if self.old_source_names_by_name:
                returned_row[RETURNING_OLD_VALUES_KEY] = {
                    name: self.get_value(
                        [
                            types.get_python_value(
                                fields_map[source_name],
                                row[f"{RETURNING_OLD_COLUMN_ALIAS_PREFIX}{projection[source_name]}"],
                            )
                            for source_name in source_names
                        ]
                    )
                    for name, source_names in self.old_source_names_by_name.items()
                }
            rows.append(returned_row)
        return rows

    def get_primary_keys(self, types: TypeRegistry, raw_rows: Sequence[Any]) -> list[Any]:
        """The primary keys of ``RETURNING`` rows.

        Args:
            types: The connection's type registry.
            raw_rows: The rows, by column name.

        Returns:
            A key per row - a tuple for a composite key.
        """
        meta = self.model._meta
        key_fields: list[tuple[Field[Any], str]] = [
            (meta.fields_map[name], meta.fields_db_projection[name]) for name in meta.primary_key_attribute_names
        ]
        keys: list[Any] = []
        for raw_row in raw_rows:
            row = dict(raw_row)
            key_values = [types.get_python_value(field, row[column]) for field, column in key_fields]
            keys.append(key_values[0] if len(key_values) == 1 else tuple(key_values))
        return keys

    @staticmethod
    def get_value(values: list[Any]) -> Any:
        """The value of a returned name - its one field's, or a tuple of a composite key's; None
        when every part is None."""
        if len(values) == 1:
            return values[0]
        return None if all(value is None for value in values) else tuple(values)

    async def read(self, connection: DatabaseClient, visibility: RowVisibility, keys: Sequence[Any]) -> list[Any]:
        """The rows of ``keys`` as they are now - instances, or dicts of the named fields - in the
        order of the keys; a key whose row is gone is left out.

        Args:
            connection: The connection - the write's transaction.
            visibility: The visibility of the write - soft-deleted rows are read too.
            keys: The primary keys.

        Returns:
            The rows.
        """
        from hare.query.queryset.queryset import QuerySet

        if not keys:
            return []
        meta = self.model._meta
        queryset: QuerySet[Any] = QuerySet(self.model).filter(pk__in=list(keys))
        queryset._apply_connection(connection)
        queryset._connection_explicitly_chosen = True
        queryset._visibility = replace(visibility, include_deleted=True, only_deleted=False)
        if self.returns_instances:
            instances_by_key = {instance.pk: instance for instance in await queryset}
            return [instances_by_key[key] for key in keys if key in instances_by_key]
        source_names = list(
            dict.fromkeys(
                [
                    *meta.primary_key_attribute_names,
                    *(source_name for names in self.source_names_by_name.values() for source_name in names),
                ]
            )
        )
        rows_by_key: dict[Any, dict[str, Any]] = {}
        for values in await queryset.values_list(*source_names):
            values_by_source_name = dict(zip(source_names, values, strict=True))
            key = self.get_value([values_by_source_name[name] for name in meta.primary_key_attribute_names])
            rows_by_key[key] = {
                name: self.get_value([values_by_source_name[source_name] for source_name in names])
                for name, names in self.source_names_by_name.items()
            }
        return [rows_by_key[key] for key in keys if key in rows_by_key]
