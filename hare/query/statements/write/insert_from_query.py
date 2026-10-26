from __future__ import annotations

import dataclasses
from collections.abc import Generator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.instrumentation.capture.captured_write import CapturedWrite
from hare.instrumentation.enums import RowOperation
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.rules.constants import (
    INSERT_FROM_METHOD_NAME,
    REFUSED_FIELD_MESSAGE,
    SOURCE_ROWS_BLIND_INDEX_REASON,
)
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.models.write.rules.tenant_write_rules import TenantWriteRules
from hare.models.write.rules.written_target_checks import WrittenTargetChecks
from hare.models.write.update.expression_assignment import ExpressionAssignment
from hare.models.write.write_steps import WriteSteps
from hare.query.expressions.expression import Expression
from hare.query.expressions.value import Value
from hare.query.query_connection import QueryConnection
from hare.query.statements.constants import INSERT_FROM_CONSTANT_PREFIX
from hare.sql.terms.parameters.query_parameters import QueryParameters
from hare.time import Timezone

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
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

    __slots__ = ("target", "source", "field_names", "columns", "written_fields", "_rules")

    def __init__(self, target: QuerySet[Any, Any], source: Any, fields: Sequence[str]) -> None:
        from hare.query.queryset.queryset import QuerySet

        if not isinstance(source, QuerySet):
            raise QueryError(f"insert_from() takes a queryset of the rows to write, got {source!r}")
        if not isinstance(fields, (list, tuple)) or not fields or not all(isinstance(name, str) for name in fields):
            raise QueryError(
                f"insert_from(fields=...) takes the names of the written fields, got "
                f"{ValueDescriptions.get_shown_value(fields)}"
            )
        self.target = target
        self.field_names = tuple(fields)
        model = target.model
        meta = model._meta
        #: What the insert writes - the fields refused, the stamps of the fields not named.
        self._rules = RowWriteRules.get(
            model,
            WriteType.INSERT,
            self.field_names,
            INSERT_FROM_METHOD_NAME,
            stamps_named_fields=False,
            single_column_relations=True,
        )
        if self._rules.blind_indexes:
            # The index is the HMAC of the plaintext, computed in Python - the source rows never reach it.
            source_name = self._rules.blind_indexes[0][0]
            raise FieldError(
                REFUSED_FIELD_MESSAGE.format(
                    name=source_name, method=INSERT_FROM_METHOD_NAME, reason=SOURCE_ROWS_BLIND_INDEX_REASON
                )
            )
        #: The field holding the column of each named field, in order.
        self.written_fields = [
            meta.fields_map[
                RowWriteRules.get_stored_field_names(
                    model, [name], INSERT_FROM_METHOD_NAME, expands_generic_keys=False, single_column_relations=True
                )[0]
            ]
            for name in self.field_names
        ]
        #: The written column of each field, in order.
        self.columns = [meta.fields_db_projection[field.model_field_name] for field in self.written_fields]
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
        constants: list[tuple[str, Field[Any], Any]] = [
            (meta.fields_db_projection[name], field, value)
            for name, field, value in self._rules.get_stamp_values(Timezone.now())
        ]
        TenantWriteRules.raise_if_source_tenants_unchecked(
            model, written, self.target._visibility.all_tenants, INSERT_FROM_METHOD_NAME
        )
        if (
            (tenant_field := meta.tenant_field) is not None
            and meta.fields_db_projection[tenant_field] not in written
            and (not self.target._visibility.all_tenants or Tenancy.get_scope(model) is not None)
        ):
            values: dict[str, Any] = {}
            TenantWriteRules.fill_new_values(model, values, "insert_from")
            constants.append(
                (meta.fields_db_projection[tenant_field], meta.fields_map[tenant_field], values[tenant_field])
            )
        return constants

    async def _insert_captured_by_reading(
        self,
        connection: DatabaseClient,
        capture_needs: CaptureNeeds,
        insert_sql: str,
        select_sql: str,
        parameters: Sequence[Any],
        columns: Sequence[str],
    ) -> int | None:
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
        captured = CapturedWrite(model, capture_needs, connection, RowOperation.INSERT)
        async with captured.transaction() as transaction_connection:
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
            await captured.capture_read({}, pks)
        if count:
            await WriteSteps.report(connection, model, RowOperation.INSERT, pks=pks)
        return count

    def hide_sensitive_parameters(self, parameters: list[Any], columns: list[str]) -> list[Any]:
        """The parameters of the insert, every one hidden (``QueryParameters``) when a sensitive
        field is written - which expression of the source a parameter feeds isn't known.

        Args:
            parameters: The parameters.
            columns: The columns written.

        Returns:
            The parameters.
        """
        meta = self.target.model._meta
        if not parameters or not meta.sensitive_fields:
            return parameters
        # A relation's columns are its key fields'.
        sensitive_columns = {
            meta.fields_db_projection[column_field_name]
            for name in meta.sensitive_fields
            for column_field_name in (getattr(meta.fields_map[name], "source_fields", None) or (name,))
            if column_field_name in meta.fields_db_projection
        }
        if sensitive_columns.isdisjoint(columns):
            return parameters
        return QueryParameters(parameters, range(len(parameters)))

    def raise_if_literals_refused(self, source: QuerySet[Any, Any], dialect: Dialect) -> None:
        """Refuses a literal the source selects which the field it is written into refuses - as
        ``update()`` refuses it: a ``Value(...)`` as the value itself, a literal an expression may be
        (``Coalesce(Value(...), ...)``) too. The rows the source reads are the database's own.

        Args:
            source: The source queryset - each branch of a ``union()`` (or of another set operation) checked.
            dialect: The dialect the rows are written on.

        Raises:
            ValidationError: A field refuses a literal.
            FieldError: A moment is written into a wall clock in no zone.
        """
        if source._combination is not None:
            for branch in source._combination.branches:
                self.raise_if_literals_refused(branch, dialect)
            return
        if source._selection is None:
            return
        selected_names = source._selection.get_selected_names()
        for name, field_name, field in zip(selected_names, self.field_names, self.written_fields, strict=False):
            expression = source._annotations.get(name)
            if type(expression) is Value:
                literal = ExpressionAssignment.get_assigned_value(expression)
                if literal is not None:
                    dialect.types.get_db_value(field, literal, None)
            elif isinstance(expression, Expression):
                ExpressionAssignment.raise_if_literal_refused(field_name, field, expression, dialect)

    @staticmethod
    def get_selects_in_selected_order(selects: list[Any], selected_names: list[str]) -> list[Any]:
        """The columns of a ``values()``/``values_list()`` source in the order of its names - its
        SQL selects the model's fields before its annotations, whatever order the names put them in.

        Args:
            selects: The source's selected columns.
            selected_names: The names the source selects, in order.

        Returns:
            The columns, each at the position of its name - ``values()`` aliases a column by its name,
            ``values_list()`` by its position. As selected when an alias is neither.
        """
        positions_by_alias = {name: position for position, name in enumerate(selected_names)}
        positions: list[int] = []
        for select in selects:
            alias = select.alias
            if alias in positions_by_alias:
                positions.append(positions_by_alias[alias])
            elif isinstance(alias, str) and alias.isdigit():
                positions.append(int(alias))
            else:
                return selects
        if sorted(positions) != list(range(len(selects))):
            return selects
        return [select for _position, select in sorted(zip(positions, selects, strict=True), key=lambda pair: pair[0])]

    @staticmethod
    def get_source_with_constants(source: QuerySet[Any, Any], constants: dict[str, Value]) -> QuerySet[Any, Any]:
        """The source selecting the constant columns after its own - each branch of a ``union()`` (or
        of another set operation) selects them, as a combined queryset takes no annotation of its own;
        a value the same in every row leaves which rows the operation keeps unchanged.

        Args:
            source: The source queryset.
            constants: The constant columns by their names.

        Returns:
            The source.
        """
        combination = source._combination
        if combination is None:
            return source.annotate(**constants)
        combined_source = source._clone()
        combined_source._combination = dataclasses.replace(
            combination,
            branches=tuple(
                InsertFromQuery.get_source_with_constants(branch, constants) for branch in combination.branches
            ),
        )
        return combined_source

    def __await__(self) -> Generator[Any, None, int | None]:
        return self._execute().__await__()

    async def _execute(self) -> int | None:
        """Runs the ``INSERT ... SELECT`` and reports it.

        Returns:
            The number of rows written.
        """
        model = self.target.model
        execution_query = self.target._get_execution_query(for_write=True)
        WrittenTargetChecks.raise_if_refused_calls(execution_query)
        connection = execution_query._connection
        self.raise_if_literals_refused(self.source, connection.dialect)
        constant_columns = self.get_constant_columns()
        source = self.source
        if constant_columns:
            source = self.get_source_with_constants(
                source,
                {
                    f"{INSERT_FROM_CONSTANT_PREFIX}{index}": Value(
                        connection.dialect.types.get_db_value(field, value, None)
                    )
                    for index, (_column, field, value) in enumerate(constant_columns)
                },
            )
        source_query = QueryConnection.get_bound_to(
            source._get_compiler(), connection, model, "the source of insert_from()"
        )
        source_query._make_subquery()
        built_query = source_query.query
        if source._selection is not None and source._combination is None:
            built_query._selects = self.get_selects_in_selected_order(
                built_query._selects, source._selection.get_selected_names()
            )
        selected_count = len(built_query._selects)
        columns = [*self.columns, *(column for column, _field, _value in constant_columns)]
        if selected_count != len(columns):
            raise QueryError(
                f"insert_from() writes {len(self.columns)} fields ({', '.join(self.field_names)}), but the source "
                f"queryset selects {selected_count - len(constant_columns)} columns"
            )
        select_sql, parameters = built_query.get_parameterized_sql()
        parameters = self.hide_sensitive_parameters(parameters, columns)
        insert_query = connection.query_class.into(model._meta.basetable).columns(*columns)
        insert_query._rows_source_sql = select_sql
        return await WriteSteps.get_whole_run(
            connection, self._insert(connection, insert_query, select_sql, parameters, columns)
        )

    async def _insert(
        self,
        connection: DatabaseClient,
        insert_query: Any,
        select_sql: str,
        parameters: Sequence[Any],
        columns: list[str],
    ) -> int | None:
        """Runs the ``INSERT ... SELECT`` and reports it.

        Args:
            connection: The connection of the insert.
            insert_query: The insert.
            select_sql: Its ``SELECT``.
            parameters: The parameters of both.
            columns: The columns written, in the order the source selects them.

        Returns:
            The number of rows written.
        """
        model = self.target.model
        capture_needs = model._meta.change_capture_needs
        if (
            capture_needs is not None
            and capture_needs.captures(RowOperation.INSERT)
            and not connection.features.supports_returning
        ):
            return await WriteSteps.run_whole(
                connection,
                self._insert_captured_by_reading(
                    connection, capture_needs, insert_query.get_sql(), select_sql, parameters, columns
                ),
            )
        if capture_needs is not None and capture_needs.captures(RowOperation.INSERT):
            # The inserted rows' keys and captured fields, captured with them in one transaction.
            insert_query = insert_query.returning(*capture_needs.columns)
            captured = CapturedWrite(model, capture_needs, connection, RowOperation.INSERT)
            async with captured.transaction() as transaction_connection:
                count, returned_rows = await transaction_connection.execute(
                    insert_query.get_sql(), cast("list[Any]", parameters), returns_rows=True
                )
                changes = await captured.capture_returned(returned_rows)
            if count:
                await WriteSteps.report(connection, model, RowOperation.INSERT, pks=[change.pk for change in changes])
            return count
        count = (await connection.execute(insert_query.get_sql(), cast("list[Any]", parameters), returns_rows=False))[
            0
        ]
        if count:
            await WriteSteps.report(connection, model, RowOperation.INSERT)
        return count
