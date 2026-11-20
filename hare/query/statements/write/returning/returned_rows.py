from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from hare.dialects.base.results.statement_result import StatementResult
from hare.exceptions import FieldError
from hare.models.write.returned_values import ReturnedValues
from hare.models.write.rules.constants import RETURNING_METHOD_NAME
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.query.hare_column_names import HareColumnNames
from hare.query.key_columns import KeyColumns
from hare.query.rows.row_layout import RowLayout
from hare.query.statements.constants import RETURNING_OLD_VALUES_KEY
from hare.sql.terms.values.old_row_value import OldRowValue

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models.model_binding import ModelBinding
    from hare.query.scopes.row_visibility import RowVisibility


class ReturnedRows:
    """What a write's ``returning()`` gives back for each row it wrote - a dict of the named fields,
    or the model instance when no field is named.

    Args:
        binding: The written model's structural binding.
        field_names: The fields - a concrete field, a forward relation (its key, a tuple for a
            composite one) or ``pk``; none for the model instances.
        old_field_names: The fields returned as they were before the write too, under ``"old"`` -
            needs ``field_names``.

    Raises:
        FieldError: A name is given twice, or isn't a concrete field, a forward relation or ``pk``;
            ``old_field_names`` without ``field_names``, or ``"old"`` among ``field_names`` with them.
    """

    __slots__ = ("_binding", "field_names", "source_names_by_name", "old_source_names_by_name")

    def __init__(
        self, binding: ModelBinding, field_names: tuple[str, ...], old_field_names: Sequence[str] = ()
    ) -> None:
        old_field_names = tuple(old_field_names)
        self._binding = binding
        self.field_names = field_names
        #: The concrete fields each name reads - one, or the key fields of a composite one.
        self.source_names_by_name: dict[str, tuple[str, ...]] = {}
        for name in field_names:
            if name in self.source_names_by_name:
                raise FieldError(f"returning() names {name!r} twice")
            self.source_names_by_name[name] = self.get_source_names(binding, name)
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
            self.old_source_names_by_name[name] = self.get_source_names(binding, name)

    @property
    def plan_key(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """What the returned rows hold - part of a plan's key."""
        return self.field_names, tuple(self.old_source_names_by_name)

    def get_call_signature_type_part(self, type_part: tuple[Any, Sequence[Any]]) -> tuple[Any, Sequence[Any]]:
        """The part of a write's call-signature key these rows add.

        Args:
            type_part: The write's own structure and values.

        Returns:
            The structure and values - ``(None, ())`` for a write keeping no plan by its calls.
        """
        structure, values = type_part
        if structure is None:
            return None, ()
        return (structure, ("returning", self.plan_key)), values

    def get_old_value_terms(self) -> list[OldRowValue]:
        """The values of the columns before the write the ``RETURNING`` returns.

        Returns:
            A term per column, each once.
        """
        meta = self._binding
        columns = dict.fromkeys(
            meta.fields_db_projection[source_name]
            for source_names in self.old_source_names_by_name.values()
            for source_name in source_names
        )
        old_value_names = HareColumnNames.get_old_value_names(self._binding)
        return [OldRowValue(column, alias=old_value_names[column]) for column in columns]

    @staticmethod
    def get_source_names(binding: ModelBinding, name: Any) -> tuple[str, ...]:
        """The concrete fields a returned name reads.

        Args:
            binding: The written model's structural binding.
            name: The name.

        Returns:
            The field names, in key order.

        Raises:
            FieldError: The name isn't a concrete field, a forward relation or ``pk``.
        """
        return tuple(
            RowWriteRules.get_stored_field_names(binding, [name], RETURNING_METHOD_NAME, expands_generic_keys=False)
        )

    @property
    def returns_instances(self) -> bool:
        """Whether the write returns its model instances - no field is named."""
        return not self.field_names

    @property
    def builds_instances_from_rows(self) -> bool:
        """Whether the instances are built from the ``RETURNING`` rows themselves - a model without a
        primary key has no key to read them back by."""
        return not self.field_names and not self._binding.has_primary_key

    def get_returning_columns(self) -> list[str]:
        """The columns the write's ``RETURNING`` returns - the named fields', or for the instances the
        primary key's, every column of a model without one.

        Returns:
            The columns, each once.
        """
        meta = self._binding
        if self.builds_instances_from_rows:
            return list(meta.db_fields)
        source_names = (
            meta.primary_key_attribute_names
            if self.returns_instances
            else [source_name for names in self.source_names_by_name.values() for source_name in names]
        )
        return list(dict.fromkeys(meta.fields_db_projection[source_name] for source_name in source_names))

    async def get_instances(
        self, connection: DatabaseClient, raw_rows: Sequence[Any], *, context: HareContext | None = None
    ) -> list[Any]:
        """The instances of ``RETURNING`` rows holding every column of the model.

        Args:
            connection: The connection the rows were written on.
            raw_rows: The rows, by column name.
            context: The application context attached to returned instances, if supplied.

        Returns:
            An instance per row.
        """
        from hare.query.rows.model_rows.model_rows import ModelRows

        if not raw_rows:
            return []
        instances = ModelRows(self._binding, connection.dialect.types).read_all(
            connection,
            StatementResult(len(raw_rows), raw_rows),
            context=context,
        )
        return instances if isinstance(instances, list) else await instances

    def get_rows(self, types: TypeRegistry, raw_rows: Sequence[Any]) -> list[dict[str, Any]]:
        """The named fields of ``RETURNING`` rows.

        Args:
            types: The connection's type registry.
            raw_rows: The rows, by column name.

        Returns:
            A dict per row, by returned name.
        """
        projection = self._binding.fields_db_projection
        layout = self._get_layout(self.source_names_by_name, types=types, column_names=projection)
        old_layout = None
        if self.old_source_names_by_name:
            old_value_names = HareColumnNames.get_old_value_names(self._binding)
            old_layout = self._get_layout(
                self.old_source_names_by_name,
                types=types,
                column_names={name: old_value_names[column] for name, column in projection.items()},
            )
        rows: list[dict[str, Any]] = []
        for raw_row in raw_rows:
            returned_row = dict(zip(self.source_names_by_name, layout.read(raw_row), strict=True))
            if old_layout is not None:
                returned_row[RETURNING_OLD_VALUES_KEY] = dict(
                    zip(self.old_source_names_by_name, old_layout.read(raw_row), strict=True)
                )
            rows.append(returned_row)
        return rows

    def _get_layout(
        self,
        source_names_by_name: Mapping[str, tuple[str, ...]],
        *,
        types: TypeRegistry | None = None,
        column_names: Mapping[str, str] | None = None,
    ) -> RowLayout:
        """Bind the columns and composite groups of the named returned values.

        Raw RETURNING rows use database column names and field readers. Follow-up SELECT
        rows already contain Python values under field names and omit both options.
        """
        fields = self._binding.fields_map
        return RowLayout(
            tuple(
                (
                    column_names[name] if column_names is not None else name,
                    types.get_python_reader(fields[name]) if types is not None else None,
                )
                for names in source_names_by_name.values()
                for name in names
            ),
            tuple(len(names) for names in source_names_by_name.values()),
        )

    def get_primary_keys(self, types: TypeRegistry, raw_rows: Sequence[Any]) -> list[Any]:
        """The primary keys of ``RETURNING`` rows.

        Args:
            types: The connection's type registry.
            raw_rows: The rows, by column name.

        Returns:
            A key per row - a tuple for a composite key.
        """
        meta = self._binding
        names = meta.primary_key_attribute_names
        return [
            values[0] if len(values) == 1 else values
            for values in KeyColumns.read_rows(
                [meta.fields_map[name] for name in names],
                [meta.fields_db_projection[name] for name in names],
                raw_rows,
                types,
            )
        ]

    async def read(
        self,
        connection: DatabaseClient,
        visibility: RowVisibility,
        keys: Sequence[Any],
        *,
        context: HareContext | None = None,
    ) -> list[Any]:
        """The rows of ``keys`` as they are now - instances, or dicts of the named fields - in the
        order of the keys; a key whose row is gone is left out.

        Args:
            connection: The connection - the write's transaction.
            visibility: The visibility of the write - soft-deleted rows are read too.
            keys: The primary keys.
            context: The application context used to load and bind returned instances.

        Returns:
            The rows.
        """
        from hare.query.queryset.queryset import QuerySet

        if not keys:
            return []
        meta = self._binding
        queryset: QuerySet[Any] = QuerySet.from_binding(self._binding, context=context).filter(pk__in=list(keys))
        queryset._apply_connection(connection)
        queryset._connection_explicitly_chosen = True
        queryset._visibility = replace(visibility, include_deleted=True, only_deleted=False)
        if self.returns_instances:
            instances_by_key = {ReturnedValues.get_hashable(instance.pk): instance for instance in await queryset}
            return [instances_by_key[key] for key in map(ReturnedValues.get_hashable, keys) if key in instances_by_key]
        primary_key_names = meta.primary_key_attribute_names
        source_names = list(
            dict.fromkeys(
                [
                    *primary_key_names,
                    *(source_name for names in self.source_names_by_name.values() for source_name in names),
                ]
            )
        )
        key_layout = RowLayout(
            tuple((name, None) for name in primary_key_names),
            (len(primary_key_names),),
        )
        output_layout = self._get_layout(self.source_names_by_name)
        rows_by_key: dict[Any, dict[str, Any]] = {}
        for values in await queryset.values(*source_names):
            key = ReturnedValues.get_hashable(key_layout.read(values)[0])
            rows_by_key[key] = dict(zip(self.source_names_by_name, output_layout.read(values), strict=True))
        return [rows_by_key[key] for key in map(ReturnedValues.get_hashable, keys) if key in rows_by_key]
