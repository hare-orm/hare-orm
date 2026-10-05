from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.enums import RowOperation
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.write_steps import WriteSteps
from hare.query.expressions.value import Value
from hare.query.query_connection import QueryConnection
from hare.query.statements.constants import INSERT_FROM_CONSTANT_PREFIX
from hare.query.statements.write.field_columns import FieldColumns
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.field import Field
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.query.queryset.queryset import QuerySet


class InsertFromQuery:
    """``Model.objects.insert_from(queryset, fields=[...])``: ``INSERT INTO <table> (<fields>) SELECT ...``
    - the rows a queryset selects written into the model's table in one statement, never read into
    Python. Awaits to the number of rows written.

    The fields not named get their database default; an ``auto_now``/``auto_now_add`` field the moment
    of the insert, and ``Meta.tenant_field`` the active tenant. A Python ``default=`` isn't computed.

    Args:
        target: The queryset of the written model - its connection and tenant visibility.
        source: The rows - a ``values()``/``values_list()`` queryset selecting one column per field, in
            the fields' order, or a queryset of models selecting its own fields of these names.
        fields: The written fields - a concrete field or a forward relation to a single-column key.

    Raises:
        QueryError: ``source`` isn't a queryset; ``fields`` is empty or not a sequence of names. When
            run: the tenant can't be set from the active scope, or would be read from the source rows
            under a scope; the source selects another number of columns.
        FieldError: A field is unknown, generated, a relation to a composite key, or given twice.
    """

    __slots__ = ("target", "source", "field_names", "columns")

    def __init__(self, target: QuerySet[Any, Any], source: Any, fields: Sequence[str]) -> None:
        from hare.query.queryset.queryset import QuerySet

        if not isinstance(source, QuerySet):
            raise QueryError(f"insert_from() takes a queryset of the rows to write, got {source!r}")
        if isinstance(fields, str) or not fields or not all(isinstance(name, str) for name in fields):
            raise QueryError(f"insert_from(fields=...) takes the names of the written fields, got {fields!r}")
        self.target = target
        self.field_names = tuple(fields)
        model = target.model
        #: The written column of each field, in order.
        self.columns = [FieldColumns.get_column_field(model, name, "insert_from()")[0] for name in self.field_names]
        if len(set(self.columns)) != len(self.columns):
            raise FieldError(f"insert_from() writes a field twice: {list(self.field_names)}")
        # A union selects what its branches select.
        selects_values = source._selection is not None or source._combination is not None
        self.source: QuerySet[Any, Any] = source if selects_values else source.values_list(*self.field_names)

    def get_constant_columns(self) -> list[tuple[str, Field[Any], Any]]:
        """The columns of the fields not named that every row gets one value in - an ``auto_now`` or
        ``auto_now_add`` field's moment, the active tenant.

        Returns:
            Each column with its field and value.

        Raises:
            QueryError: See the class.
        """
        model = self.target.model
        meta = model._meta
        written = set(self.columns)
        constants: list[tuple[str, Field[Any], Any]] = []
        now = Timezone.now()
        for field_name, field in meta.fields_map.items():
            column = meta.fields_db_projection.get(field_name)
            if column is None or column in written:
                continue
            if isinstance(field, (DatetimeField, TimeField)) and (field.auto_now or field.auto_now_add):
                constants.append((column, field, field.get_auto_now_value(now)))
        self.raise_if_tenant_unchecked(written)
        if (
            (tenant_field := meta.tenant_field) is not None
            and meta.fields_db_projection[tenant_field] not in written
            and (not self.target._visibility.all_tenants or Tenancy.get_scope(model) is not None)
        ):
            values: dict[str, Any] = {}
            Tenancy.fill_create_values(model, values)
            constants.append(
                (meta.fields_db_projection[tenant_field], meta.fields_map[tenant_field], values[tenant_field])
            )
        return constants

    def raise_if_tenant_unchecked(self, written_columns: set[str]) -> None:
        """Rejects tenants read from the source rows while a scope is active - a written tenant, a
        relation to a tenant-scoped model - which the insert can't check row by row.

        Args:
            written_columns: The columns of the named fields.

        Raises:
            QueryError: A tenant would be read from the source rows under an active scope.
        """
        if self.target._visibility.all_tenants:
            return
        model = self.target.model
        meta = model._meta
        tenant_field = meta.tenant_field
        if (
            tenant_field is not None
            and meta.fields_db_projection[tenant_field] in written_columns
            and Tenancy.get_scope(model) is not None
        ):
            raise QueryError(
                f"insert_from() writes {tenant_field!r} from the source rows under the active tenant scope - "
                "leave it out to write the active tenant, or call all_tenants() on the target"
            )
        for relation_name in meta.foreign_key_fields | meta.one_to_one_fields:
            relation = cast("ForeignKeyFieldInstance[Any]", meta.fields_map[relation_name])
            related_model = relation.related_model
            if (
                related_model._meta.tenant_field
                and meta.fields_db_projection[relation.source_fields[0]] in written_columns
                and Tenancy.get_scope(related_model) is not None
            ):
                raise QueryError(
                    f"insert_from() writes {relation_name!r} from the source rows under the active tenant scope of "
                    f"{related_model.__name__} - the rows it points at can't be checked; call all_tenants() on the "
                    "target"
                )

    async def _insert_captured_by_reading(
        self,
        connection: DatabaseClient,
        capture_needs: CaptureNeeds,
        insert_sql: str,
        select_sql: str,
        parameters: Sequence[Any],
        columns: Sequence[str],
    ) -> int:
        """Runs the insert into a model with ``Meta.change_capture`` on a database whose writes return
        no rows: the source rows' keys are read first, and the inserted rows by them after the insert.

        Args:
            connection: The connection of the insert.
            capture_needs: The model's needs.
            insert_sql: The ``INSERT ... SELECT``.
            select_sql: Its ``SELECT`` alone.
            parameters: The parameters of both.
            columns: The columns written, in the order the source selects them.

        Returns:
            The number of rows written.

        Raises:
            UnSupportedError: The primary key isn't among the fields written.
        """
        model = self.target.model
        meta = model._meta
        key_columns = [meta.fields_db_projection[name] for name in meta.primary_key_attribute_names]
        if not all(column in columns for column in key_columns):
            raise UnSupportedError(
                f"insert_from() into {model.__name__}, whose Meta.change_capture reads the rows written, writes no "
                f"primary key on the {connection.dialect.name} database, which returns no rows of a write - name "
                f"the key fields ({', '.join(meta.primary_key_attribute_names)}) among fields"
            )
        positions = [columns.index(column) for column in key_columns]
        key_fields = [meta.fields_map[name] for name in meta.primary_key_attribute_names]
        types = connection.dialect.types
        async with ChangeCapturing.transaction(connection) as transaction_connection:
            _selected, source_rows = await transaction_connection.execute(
                select_sql, list(parameters), returns_rows=True
            )
            # A row by column name on some drivers - its values are in the order selected.
            rows = [list(row.values()) if isinstance(row, Mapping) else row for row in source_rows]
            key_values = [
                [
                    types.get_python_value(field, row[position])
                    for field, position in zip(key_fields, positions, strict=True)
                ]
                for row in rows
            ]
            pks = list(dict.fromkeys(values[0] if len(values) == 1 else tuple(values) for values in key_values))
            count = (await transaction_connection.execute(insert_sql, list(parameters), returns_rows=False))[0]
            read_after = await ChangeCapturing.read_values(model, transaction_connection, pks, capture_needs)
            changes = ChangeCapturing.get_read_changes(model, capture_needs, RowOperation.INSERT, {}, read_after)
            await ChangeCapturing.capture(transaction_connection, model, changes)
        if count:
            await WriteSteps.report(connection, model, RowOperation.INSERT, pks=pks)
        return count

    def __await__(self) -> Generator[Any, None, int]:
        return self._execute().__await__()

    async def _execute(self) -> int:
        """Runs the ``INSERT ... SELECT`` and reports it.

        Returns:
            The number of rows written.
        """
        model = self.target.model
        connection = self.target._get_execution_query(for_write=True)._connection
        constant_columns = self.get_constant_columns()
        source = self.source
        if constant_columns:
            source = source.annotate(
                **{
                    f"{INSERT_FROM_CONSTANT_PREFIX}{index}": Value(
                        connection.dialect.types.get_db_value(field, value, None)
                    )
                    for index, (_column, field, value) in enumerate(constant_columns)
                }
            )
        source_query = QueryConnection.get_bound_to(
            source._get_compiler(), connection, model, "the source of insert_from()"
        )
        source_query._make_subquery()
        built_query = source_query.query
        selected_count = len(built_query._selects)
        columns = [*self.columns, *(column for column, _field, _value in constant_columns)]
        if selected_count != len(columns):
            raise QueryError(
                f"insert_from() writes {len(self.columns)} fields ({', '.join(self.field_names)}), but the source "
                f"queryset selects {selected_count - len(constant_columns)} columns"
            )
        select_sql, parameters = built_query.get_parameterized_sql()
        insert_query = connection.query_class.into(model._meta.basetable).columns(*columns)
        insert_query._rows_source_sql = select_sql
        capture_needs = model._meta.change_capture_needs
        if (
            capture_needs is not None
            and capture_needs.captures(RowOperation.INSERT)
            and not connection.features.supports_returning
        ):
            return await self._insert_captured_by_reading(
                connection, capture_needs, insert_query.get_sql(), select_sql, parameters, columns
            )
        if capture_needs is not None and capture_needs.captures(RowOperation.INSERT):
            # The inserted rows' keys and captured fields, captured with them in one transaction.
            insert_query = insert_query.returning(*capture_needs.columns)
            async with ChangeCapturing.transaction(connection) as transaction_connection:
                count, returned_rows = await transaction_connection.execute(
                    insert_query.get_sql(), parameters, returns_rows=True
                )
                changes = ChangeCapturing.get_returned_changes(
                    model, capture_needs, transaction_connection.dialect.types, returned_rows, RowOperation.INSERT
                )
                await ChangeCapturing.capture(transaction_connection, model, changes)
            if count:
                await WriteSteps.report(connection, model, RowOperation.INSERT, pks=[change.pk for change in changes])
            return count
        count = (await connection.execute(insert_query.get_sql(), parameters, returns_rows=False))[0]
        if count:
            await WriteSteps.report(connection, model, RowOperation.INSERT)
        return count
