from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.routing.written_connections import WrittenConnections
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import UnSupportedError
from hare.fields.database_default import DatabaseDefault
from hare.instrumentation.enums import RowOperation
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.generated_keys import GeneratedKeys
from hare.models.write.insert.insert_statement import InsertStatement
from hare.models.write.instance_capture import InstanceCapture
from hare.models.write.instance_values import InstanceValues
from hare.models.write.returned_values import ReturnedValues
from hare.models.write.rules.constants import SAVE_METHOD_NAME
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.models.write.update.expression_assignment import ExpressionAssignment
from hare.models.write.update.update_parameter_slot import UpdateParameterSlot
from hare.models.write.write_steps import WriteSteps
from hare.query.expressions import Expression, ExpressionContext
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.functions.cast import Cast
from hare.sql.terms.parameters.parameter import Parameter
from hare.sql.terms.parameters.query_parameters import QueryParameters
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.sql import Table
    from hare.sql.terms.criteria.criterion import Criterion


class InstanceWriter:
    """Writes one model's instances on one connection: the INSERT and UPDATE of ``save()`` and the
    DELETE of ``delete()``. The statements are computed once per model and connection type.

    Args:
        model: The model.
        connection: The connection.
    """

    #: The attributes a writer computes once per model and connection type - stored in and
    #: restored from ``StatementPlans.instance_writes``.
    PER_MODEL_CACHED_ATTRIBUTES: ClassVar[tuple[str, ...]] = (
        "regular_columns",
        "insert_query",
        "regular_columns_all",
        "insert_query_all",
        "delete_query",
        "delete_query_without_tenant_scope",
        "insert_rules",
    )

    def __init__(self, model: type[Model], connection: DatabaseClient) -> None:
        self.model = model
        self.connection: DatabaseClient = connection
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection.connection_alias)
        #: Whether the writes capture their changes (``Meta.change_capture``) - off for the writer
        #: ``InstanceCapture`` runs them through.
        self.captures_changes = True
        #: The update fields and write type of the rules ``get_update_rules()`` gave last, and those rules - a
        #: save asks for them more than once.
        self.update_rules_key: tuple[tuple[str, ...] | None, WriteType] | None = None
        self.update_rules: RowWriteRules | None = None
        # The dialect, the connection's features and the builder class are part of the key: two
        # contexts may use one connection name for different databases.
        meta = model._meta
        key = (
            connection.connection_alias,
            connection.dialect,
            connection.features.plan_key,
            meta.query_class,
            meta.schema,
            meta.db_table,
        )
        #: The writer's key in ``StatementPlans.instance_writes`` - what its UPDATEs and DELETEs of
        #: other shapes are kept under too.
        self.statements_key = key
        statements = StatementPlans.instance_writes.get_for_model(model, key)
        if statements is None:
            insert_statement = InsertStatement(self.model, self.connection)
            self.regular_columns = insert_statement.get_field_names(with_primary_key=False)
            self.insert_query = self._get_insert_sql(insert_statement, self.regular_columns, primary_key_given=False)
            self.regular_columns_all = self.regular_columns
            self.insert_query_all = self.insert_query
            if self.model._meta.generated_db_fields:
                self.regular_columns_all = insert_statement.get_field_names(with_primary_key=True)
                self.insert_query_all = self._get_insert_sql(
                    insert_statement, self.regular_columns_all, primary_key_given=True
                )

            basequery = self.connection.query_class.from_(self.model._meta.basetable)
            deleted_row_db_columns = self._deleted_row_db_columns()
            pk_where_delete_builder = self._where_by_columns(basequery, deleted_row_db_columns)
            # A cascade deletes a descendant it found through a real FK match - without the
            # active-tenant condition, which could differ from the descendant's tenant.
            self.delete_query_without_tenant_scope = str(pk_where_delete_builder.delete())
            tenant_condition, _ = self._tenant_scope_where(self.model._meta.basetable, len(deleted_row_db_columns))
            delete_builder = (
                pk_where_delete_builder.where(tenant_condition)
                if tenant_condition is not None
                else pk_where_delete_builder
            )
            self.delete_query = str(delete_builder.delete())
            #: What an insert of an instance writes itself - its stamps and blind indexes.
            self.insert_rules = RowWriteRules.get(self.model, WriteType.INSERT, None, SAVE_METHOD_NAME)

            StatementPlans.instance_writes[(self.model, *key)] = tuple(
                getattr(self, name) for name in self.PER_MODEL_CACHED_ATTRIBUTES
            )
        else:
            # PER_MODEL_CACHED_ATTRIBUTES, in its order - a writer is made for every save().
            (
                self.regular_columns,
                self.insert_query,
                self.regular_columns_all,
                self.insert_query_all,
                self.delete_query,
                self.delete_query_without_tenant_scope,
                self.insert_rules,
            ) = statements

    def _where_by_columns(self, builder: QueryBuilder, db_columns: Iterable[str]) -> QueryBuilder:
        """Adds one ``column = $N`` condition per column, in the order of ``db_columns`` - the WHERE of a
        statement writing the row by its key columns.

        Args:
            builder: The statement.
            db_columns: The columns, each compared with the next parameter.

        Returns:
            The statement with the conditions.
        """
        table = self.model._meta.basetable
        for position, db_column in enumerate(db_columns):
            builder = builder.where(table[db_column] == self.parameter(position))
        return builder

    @staticmethod
    def parameter(position: int) -> Parameter:
        """The placeholder of a statement's parameter.

        Args:
            position: The parameter's place among the statement's values, from 0.

        Returns:
            The placeholder.
        """
        return Parameter(index=position + 1)

    def _pk_db_columns(self) -> list[str]:
        """The DB column name(s) making up the PK, in ``primary_key_attribute`` order - one for a single-column
        PK, one per column for a composite one."""
        if self.model._meta.has_composite_primary_key:
            return [self.model._meta.fields_db_projection[name] for name in self.model._meta.primary_key_attribute]
        return [self.model._meta.db_pk_column]

    def _deleted_row_db_columns(self) -> list[str]:
        """The DB columns a DELETE of one row finds it by - the PK's, and
        ``Meta.optimistic_lock_field``'s: a row changed since the instance read it isn't deleted."""
        pk_db_columns = self._pk_db_columns()
        optimistic_lock_field = self.model._meta.optimistic_lock_field
        if optimistic_lock_field is None:
            return pk_db_columns
        return [*pk_db_columns, self.model._meta.fields_db_projection[optimistic_lock_field]]

    def _deleted_row_where_values(self, instance: type[Model] | Model) -> list[Any]:
        """The bound values of ``_deleted_row_db_columns()``, in its order."""
        values = self._pk_where_values(instance)
        optimistic_lock_field = self.model._meta.optimistic_lock_field
        if optimistic_lock_field is not None:
            values.append(
                self.connection.dialect.types.get_db_value(
                    self.model._meta.fields_map[optimistic_lock_field],
                    getattr(instance, optimistic_lock_field),
                    instance,
                )
            )
        return values

    def _pk_where_values(self, instance: type[Model] | Model) -> list[Any]:
        """The bound value(s) for a WHERE-by-pk clause, in the same column order
        ``get_update_sql``/``delete_query`` build it in - one value for a single-column PK, one
        per column (in ``primary_key_attribute`` order) for a composite one."""
        if self.model._meta.has_composite_primary_key:
            pk_values = cast("tuple[Any, ...]", instance.pk)
            return [
                self.connection.dialect.types.get_db_value(self.model._meta.fields_map[pk_name], pk_value, instance)
                for pk_name, pk_value in zip(self.model._meta.primary_key_attribute, pk_values, strict=True)
            ]
        return [self.connection.dialect.types.get_db_value(self.model._meta.pk, instance.pk, instance)]

    def _tenant_scope_where(
        self,
        table: Table,
        next_index: int,
        parameter_factory: Callable[[int], Term] | None = None,
        tenant_value_count: int = 1,
    ) -> tuple[Criterion | None, int]:
        """The condition limiting a single-row write to the tenants of the model's active scope -
        whatever tenant the instance itself claims.

        One value or none takes two slots bound to the same value: ``(tenant_col = value) OR (value
        IS NULL)`` - no condition at all when the rows aren't limited. Several values take a slot
        each: ``tenant_col IN (values)``.

        Args:
            table: The written table.
            next_index: The 0-based position of the first bound value.
            parameter_factory: Builds a placeholder term for a position; ``self.parameter`` by
                default.
            tenant_value_count: How many tenant values the scope has - 1 for one or none.

        Returns:
            The condition and the next free position; ``(None, next_index)`` without
            ``Meta.tenant_field``.
        """
        tenant_field = self.model._meta.tenant_field
        if not tenant_field:
            return None, next_index
        parameter = parameter_factory or self.parameter
        tenant_db_column = self.model._meta.fields_db_projection[tenant_field]
        if tenant_value_count > 1:
            placeholders = [parameter(next_index + position) for position in range(tenant_value_count)]
            return table[tenant_db_column].isin(placeholders), next_index + tenant_value_count
        second_parameter: Term = parameter(next_index + 1)
        # A parameter used only in IS NULL gives the database no type to infer - the dialect names
        # the cast.
        sql_type = self.connection.dialect.parameters.get_null_check_cast_type(
            self.model._meta.fields_map[tenant_field]
        )
        if sql_type is not None:
            second_parameter = Cast(second_parameter, RawSQLTerm(sql_type))
        condition = (table[tenant_db_column] == parameter(next_index)) | second_parameter.isnull()
        return condition, next_index + 2

    def _get_tenant_scope_guard(self) -> tuple[int, list[Any]]:
        """What ``_tenant_scope_where()`` is built and bound with for the model's active scope.

        Returns:
            ``(tenant value count, bound values)``: for several values - their count and the
            values; for one value or unlimited rows - 1 and the value (or None) twice;
            ``(1, [])`` if the model has no ``Meta.tenant_field``.
        """
        tenant_field = self.model._meta.tenant_field
        if not tenant_field:
            return 1, []
        tenant_values = Tenancy.get_values(Tenancy.get_scope(self.model))
        if tenant_values is None:
            return 1, [None, None]
        field = self.model._meta.fields_map[tenant_field]
        get_db_value = self.connection.dialect.types.get_db_value
        db_values = [get_db_value(field, value, self.model) for value in tenant_values]
        if len(db_values) > 1:
            return len(db_values), db_values
        return 1, db_values * 2

    def _get_insert_values(self, obj: Model, columns: list[str]) -> list[Any]:
        """The values an INSERT of ``obj`` binds.

        Args:
            obj: The obj inserted.
            columns: The column list, one of this writer's own.

        Returns:
            One value per column.
        """
        return BulkWriteBatches.serialize_instance(self.model, self.connection.dialect.types, obj, columns)

    @staticmethod
    def _get_insert_sql(
        insert_statement: InsertStatement,
        field_names: list[str],
        *,
        primary_key_given: bool,
        omitted_field_names: list[str] | tuple[str, ...] = (),
    ) -> str:
        """The single-row INSERT of fields, returning what the database gives the row.

        Args:
            insert_statement: The model's INSERT builder.
            field_names: The written fields.
            primary_key_given: The row carries its primary key.
            omitted_field_names: Fields left to their database default.

        Returns:
            The SQL.
        """
        columns = insert_statement.get_columns(field_names)
        return str(
            insert_statement.get_query(
                columns,
                [insert_statement.get_parameter_row(len(columns))] if columns else [],
                returning=insert_statement.get_returning_columns(
                    primary_key_given=primary_key_given, omitted_field_names=omitted_field_names
                ),
            )
        )

    def hide_sensitive_values(
        self, values: list[Any], field_names: list[str], parameter_layout: list[Any] | None = None
    ) -> list[Any]:
        """The values of a write, the ``sensitive=True`` fields' never shown (``QueryParameters``).

        Args:
            values: The bound values - the fields' values first, in the order of ``field_names``.
            field_names: The written fields.
            parameter_layout: The layout the values were bound by (``UpdateParameterSlot.bind()``),
                None when they bind as they are.

        Returns:
            The values - a ``QueryParameters`` when a sensitive field is written.
        """
        sensitive_fields = self.model._meta.sensitive_fields
        hidden_positions = {position for position, name in enumerate(field_names) if name in sensitive_fields}
        if not hidden_positions:
            return values
        if parameter_layout is not None:
            hidden_positions = {
                index
                for index, parameter in enumerate(parameter_layout)
                if isinstance(parameter, UpdateParameterSlot) and parameter.position in hidden_positions
            }
        return QueryParameters(values, hidden_positions)

    async def _execute_insert_statement(self, query: str, values: list[Any]) -> Any:
        """Runs one INSERT.

        Args:
            query: The INSERT, with a RETURNING clause when the database generates columns.
            values: Its parameters.

        Returns:
            The row the INSERT returned; without one, the key the database gave the row where the
            driver reports it, else None.
        """
        result = await self.connection.execute(query, values, returns_rows=True)
        rows = result[1]
        if rows:
            return rows[0]
        return result.inserted_id

    def _process_insert_result(self, obj: Model, results: Any) -> None:
        """Sets the values an INSERT returned - the columns the database generated - on the
        obj.

        Args:
            obj: The inserted obj.
            results: The row the INSERT returned, the key the database gave the row (a database
                without ``RETURNING``), or None.
        """
        if results is None:
            return
        meta = self.model._meta
        if not hasattr(results, "keys"):
            # The key of a row inserted without RETURNING - set when the database generated it.
            pk_field = meta.pk
            primary_key_attribute = cast("str", meta.primary_key_attribute)
            if pk_field is not None and pk_field.generated and getattr(obj, primary_key_attribute) is None:
                setattr(obj, primary_key_attribute, self.connection.dialect.types.get_python_value(pk_field, results))
            return
        ReturnedValues.apply_row(self.model, self.connection.dialect.types, obj, results, connection=self.connection)

    def _get_insert_omitting_defaults(
        self, obj: Model, field_names: list[str], primary_key_given: bool
    ) -> tuple[str, list[Any]]:
        """The INSERT of an obj leaving the fields holding ``DatabaseDefault`` to the database, and
        its values - a refused value raises here.

        Args:
            obj: The obj.
            field_names: The fields an INSERT writes.
            primary_key_given: The obj carries its primary key.

        Returns:
            The SQL and its values.
        """
        omitted_field_names = [
            field_name for field_name in field_names if isinstance(getattr(obj, field_name), DatabaseDefault)
        ]
        written_field_names = [field_name for field_name in field_names if field_name not in omitted_field_names]
        fields_map = self.model._meta.fields_map
        get_db_value = self.connection.dialect.types.get_db_value
        values = [
            get_db_value(fields_map[field_name], getattr(obj, field_name), obj) for field_name in written_field_names
        ]
        if self.model._meta.sensitive_fields:
            values = self.hide_sensitive_values(values, written_field_names)
        sql = self._get_insert_sql(
            InsertStatement(self.model, self.connection),
            written_field_names,
            primary_key_given=primary_key_given,
            omitted_field_names=omitted_field_names,
        )
        return sql, values

    def _has_db_default_values(self, obj: Model, field_names: list[str]) -> bool:
        """Whether a field of the obj holds ``DatabaseDefault``."""
        if not self.model._meta.db_default_db_columns:
            return False
        for field_name in field_names:
            if isinstance(getattr(obj, field_name, None), DatabaseDefault):
                return True
        return False

    async def execute_insert(self, obj: Model) -> None:
        """Inserts the obj's row and sets on it what the database gave the row - where the database
        returns no row, the fields it gave values to are left unloaded.

        Args:
            obj: The obj.

        Raises:
            UnSupportedError: A field of the primary key is left to its ``db_default`` on a database
                returning no row - the obj could never know its key.
        """
        capture_needs = self.model._meta.change_capture_needs
        if capture_needs is not None and self.captures_changes and capture_needs.captures(RowOperation.INSERT):
            await WriteSteps.run_whole(self.connection, InstanceCapture.insert(self, obj, capture_needs))
            return
        features = self.connection.features
        series_keyed: list[Model] = []
        stamped_values = InstanceValues()
        if not features.supports_returning:
            self.raise_if_key_left_to_database(obj)
        try:
            self.insert_rules.stamp((obj,), stamped_values)
            if features.takes_keys_before_insert and not obj._custom_generated_pk:
                series_keyed = await GeneratedKeys.assign(self.model, self.connection, (obj,))
            primary_key_given = obj._custom_generated_pk
            field_names, insert_sql = (
                (self.regular_columns_all, self.insert_query_all)
                if primary_key_given
                else (self.regular_columns, self.insert_query)
            )
            if self._has_db_default_values(obj, field_names):
                omitting_sql, omitting_values = self._get_insert_omitting_defaults(obj, field_names, primary_key_given)
                insert_result = await self._execute_insert_statement(omitting_sql, omitting_values)
                stamped_values.keep(self.connection)
                self._process_insert_result(obj, insert_result)
                if not features.supports_returning:
                    ReturnedValues.leave_unloaded(
                        self.model, (obj,), self._get_defaulted_columns(obj), connection=self.connection
                    )
                return
            insert_values = self._get_insert_values(obj, field_names)
            if self.model._meta.sensitive_fields:
                insert_values = self.hide_sensitive_values(insert_values, field_names)
            # _execute_insert_statement(), written out - one coroutine less for every create().
            result = await self.connection.execute(insert_sql, insert_values, returns_rows=True)
        except BaseException:
            # A failed write leaves the obj as it was - without the stamps it took or the key a series
            # gave it.
            stamped_values.restore()
            if series_keyed:
                GeneratedKeys.take_back(self.model, series_keyed)
            raise
        stamped_values.keep(self.connection)
        rows = result[1]
        self._process_insert_result(obj, rows[0] if rows else result.inserted_id)
        if not features.supports_returning and self.model._meta.recomputed_db_fields:
            ReturnedValues.leave_unloaded(
                self.model, (obj,), self.model._meta.recomputed_db_fields, connection=self.connection
            )

    def raise_if_key_left_to_database(self, obj: Model) -> None:
        """Refuses an obj whose primary key takes a field's ``db_default`` on a database returning no
        row of a write - the obj could never know its key.

        Args:
            obj: The obj about to be inserted.

        Raises:
            UnSupportedError: A field of the obj's primary key holds ``DatabaseDefault``.
        """
        for name in self.model._meta.primary_key_attribute_names:
            if isinstance(getattr(obj, name, None), DatabaseDefault):
                raise UnSupportedError(
                    f"{self.model.__name__}.{name} is part of the primary key and left to its db_default - the "
                    f"{self.connection.dialect.name} database returns no row of a write, so the key can't be read "
                    "back; give it a value (default= computes one in Python)"
                )

    def _get_defaulted_columns(self, obj: Model) -> list[str]:
        """The columns of a just-inserted row whose values the database gave it: the ``db_default``
        columns the obj left to the database, and the computed ones.

        Args:
            obj: The inserted obj.

        Returns:
            The columns.
        """
        meta = self.model._meta
        db_projection_reverse = meta.fields_db_projection_reverse
        columns = [
            column
            for column in meta.db_default_db_columns
            if isinstance(getattr(obj, db_projection_reverse.get(column, column), None), DatabaseDefault)
        ]
        columns.extend(column for column in meta.recomputed_db_fields if column not in columns)
        return columns

    def get_update_sql(
        self,
        update_fields: Iterable[str] | None,
        expressions: dict[str, Expression] | None,
        apply_active_tenant_scope_guard: bool = True,
        tenant_value_count: int = 1,
        only_live_row: bool = False,
    ) -> tuple[str, list[Any] | None]:
        """The UPDATE of one row by primary key - cached per field set when no field is set from an
        expression.

        Args:
            update_fields: The fields to SET - every writable field when None or empty.
            expressions: The fields SET from an expression instead of a value, by name.
            apply_active_tenant_scope_guard: Add the active-tenant condition - False for a row a
                cascade found through a real FK match.
            tenant_value_count: How many tenant values the guard compares with - 1 for one or none.
            only_live_row: Match the row only while ``Meta.soft_delete_field`` is NULL.

        Returns:
            The SQL, and the layout its bound values are built by (``UpdateParameterSlot.bind()``) -
            None when the caller's values bind as they are.
        """
        # Materialize once: update_fields is typed Iterable[str], and reusing a
        # one-shot generator both as the cache key and in the loop below would
        # silently generate 0 fields the second time.
        fields_tuple = tuple(update_fields) if update_fields else ()
        # The UPDATE differs by the row it matches: only a live one, without the active-tenant
        # condition, or under a guard listing a placeholder per tenant value.
        if only_live_row:
            statement_key = (
                self.statements_key,
                True,
                apply_active_tenant_scope_guard,
                tenant_value_count,
                fields_tuple,
            )
        elif not apply_active_tenant_scope_guard:
            statement_key = (self.statements_key, False, False, 0, fields_tuple)
        else:
            guarded_value_count = tenant_value_count if tenant_value_count > 1 else 1
            statement_key = (self.statements_key, False, True, guarded_value_count, fields_tuple)
        if not expressions:
            cached_sql = StatementPlans.instance_updates.get_for_model(self.model, statement_key)
            if cached_sql is not None:
                return cached_sql, None
        query = self._build_update_query(
            fields_tuple,
            expressions or {},
            apply_active_tenant_scope_guard,
            tenant_value_count,
            only_live_row,
        )
        if not expressions:
            sql = query.get_sql()
            StatementPlans.instance_updates[(self.model, *statement_key)] = sql
            return sql, None
        sql, parameter_layout = query.get_parameterized_sql()
        return sql, parameter_layout

    def _build_update_query(
        self,
        fields_tuple: tuple[str, ...],
        expressions: dict[str, Expression],
        apply_active_tenant_scope_guard: bool,
        tenant_value_count: int,
        only_live_row: bool,
    ) -> QueryBuilder:
        """The UPDATE ``get_update_sql()`` renders.

        Args:
            fields_tuple: The fields to SET - every writable field when empty.
            expressions: The fields SET from an expression instead of a value, by name.
            apply_active_tenant_scope_guard: Add the active-tenant condition.
            tenant_value_count: How many tenant values the guard compares with.
            only_live_row: Match the row only while ``Meta.soft_delete_field`` is NULL.

        Returns:
            The query.
        """
        meta = self.model._meta
        parameter: Callable[[int], Term] = UpdateParameterSlot.as_term if expressions else self.parameter
        table = meta.basetable
        query = self.connection.query_class.update(table)
        parameter_index = 0
        primary_key_attribute_names = meta.primary_key_attribute_names
        for field in fields_tuple or meta.fields_db_projection.keys():
            db_column = meta.fields_db_projection[field]
            field_object = meta.fields_map[field]
            if field_object.generated:
                continue
            if field in primary_key_attribute_names:
                continue
            if field not in expressions:
                query = query.set(db_column, parameter(parameter_index))
                parameter_index += 1
            else:
                expression_term = ExpressionAssignment.get_term(
                    field,
                    field_object,
                    expressions[field],
                    ExpressionContext(
                        model=self.model,
                        dialect=self.connection.dialect,
                        connection=self.connection,
                        table=table,
                        annotations={},
                    ),
                )
                query = query.set(db_column, expression_term)

        for pk_db_column in self._pk_db_columns():
            query = query.where(table[pk_db_column] == parameter(parameter_index))
            parameter_index += 1
        if optimistic_lock_field := meta.optimistic_lock_field:
            # Optimistic locking: only matches the row if it's still at the version this instance
            # was read at - execute_update binds the pre-increment value here, and the freshly
            # incremented value as this field's own SET (it's included in effective_fields there).
            version_db_column = meta.fields_db_projection[optimistic_lock_field]
            query = query.where(table[version_db_column] == parameter(parameter_index))
            parameter_index += 1

        if apply_active_tenant_scope_guard:
            tenant_condition, _ = self._tenant_scope_where(table, parameter_index, parameter, tenant_value_count)
            if tenant_condition is not None:
                query = query.where(tenant_condition)
        if only_live_row:
            soft_delete_field = cast("str", meta.soft_delete_field)
            query = query.where(table[meta.fields_db_projection[soft_delete_field]].isnull())

        # A generated column is recomputed by the UPDATE - read back so the instance doesn't keep a
        # stale value. A generated primary key is never generated anew.
        if self.connection.features.supports_returning and (generated_fields := meta.recomputed_db_fields):
            query = query.returning(*generated_fields)
        return query

    async def execute_update(
        self,
        instance: type[Model] | Model,
        update_fields: Iterable[str] | None,
        apply_active_tenant_scope_guard: bool = True,
        only_live_row: bool = False,
        write_type: WriteType = WriteType.UPDATE,
    ) -> int | None:
        """Updates the instance's row.

        Args:
            instance: The instance to write.
            update_fields: Fields to write, or ``None`` for every writable field.
            apply_active_tenant_scope_guard: Passed straight through to ``get_update_sql`` -
                ``False`` for a cascade-discovered descendant, whose row a real FK match already
                verified.
            only_live_row: Write the row only while it isn't soft-deleted.
            write_type: ``WriteType.SOFT_DELETE`` for the write of ``Meta.soft_delete_field`` by
                ``delete()``/``restore()``.

        Returns:
            The number of matched rows; ``None`` when there was nothing to write, or the database doesn't
            report how many rows a statement wrote (``Features.reports_written_row_count``).

        Raises:
            QueryError: ``update_fields`` names a field the update can't set.
            UnSupportedError: ``update_fields`` names a key column the database doesn't update in
                place, or a full save changes one.
        """
        capture_needs = self.model._meta.change_capture_needs
        if capture_needs is not None and self.captures_changes and capture_needs.captures(RowOperation.UPDATE):
            return await WriteSteps.run_whole(
                self.connection,
                InstanceCapture.update(
                    self,
                    instance,  # type: ignore[arg-type]
                    update_fields,
                    capture_needs,
                    apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                    only_live_row=only_live_row,
                    write_type=write_type,
                ),
            )
        rules = self.get_update_rules(update_fields, write_type)
        # A field left to the database's default isn't written.
        effective_fields = [
            field for field in rules.field_names if not isinstance(getattr(instance, field), DatabaseDefault)
        ]
        model_instance: Model = instance  # type: ignore[assignment]
        effective_fields = await self._get_fields_without_fixed(model_instance, effective_fields, update_fields)
        if not effective_fields:
            return None

        # The stamps, the blind indexes and the lock bump change the instance before the outcome is known.
        changed_values = InstanceValues()
        try:
            rules.stamp((model_instance,), changed_values)
            # auto_now fields are written on every save(), whatever update_fields names.
            effective_fields.extend(name for name in rules.stamped_names if name not in effective_fields)
            optimistic_lock_field = rules.version_field_name
            old_version_value = None
            if optimistic_lock_field:
                # Bumped on every update - the old value, bound as an extra WHERE parameter by
                # get_update_sql(), makes the UPDATE match only a row nothing changed since.
                old_version_value = changed_values.bump_optimistic_lock(model_instance, optimistic_lock_field)
                if optimistic_lock_field not in effective_fields:
                    effective_fields.append(optimistic_lock_field)
            rows = await self._execute_update_statement(
                instance, effective_fields, old_version_value, apply_active_tenant_scope_guard, only_live_row
            )
            if rows == 0:
                # Nothing matched (typically a concurrent version conflict) - the row never changed.
                changed_values.restore()
            else:
                changed_values.keep(self.connection)
            return rows
        except BaseException:
            changed_values.restore()
            raise

    async def _get_fields_without_fixed(
        self, obj: Model, field_names: list[str], update_fields: Iterable[str] | None
    ) -> list[str]:
        """The written fields without those whose key columns the database doesn't update in place -
        a full save leaves them as they are, once it has read that they are.

        Args:
            obj: The instance written.
            field_names: The fields written.
            update_fields: The fields the save names, None for a full save.

        Returns:
            The fields.

        Raises:
            UnSupportedError: ``update_fields`` names such a field, or a full save changes one.
        """
        table_options = self.model._meta.get_table_options(self.connection.dialect)
        if table_options is None:
            return field_names
        if update_fields is not None:
            table_options.raise_if_fixed_fields_written(self.model, field_names)
            return field_names
        fixed_field_names = table_options.get_fixed_field_names(self.model)
        if not fixed_field_names:
            return field_names
        written_fixed_names = [name for name in field_names if name in fixed_field_names]
        if written_fixed_names:
            # The row as it stands - of a table keeping versions of a row, their merged one.
            stored_rows = await table_options.with_row_versions_merged(
                RowScopes.get_base_queryset(self.model, RowVisibility(all_tenants=True, include_deleted=True))
                .using(self.connection)
                .filter(pk=obj.pk)
            ).values_list(*written_fixed_names)
            if stored_rows:
                fields_map = self.model._meta.fields_map
                types = self.connection.dialect.types
                changed_names = [
                    name
                    for name, stored_value in zip(written_fixed_names, stored_rows[0], strict=True)
                    if types.get_db_value(fields_map[name], stored_value, obj)
                    != types.get_db_value(fields_map[name], getattr(obj, name), obj)
                ]
                table_options.raise_if_fixed_fields_written(self.model, changed_names)
        return [name for name in field_names if name not in fixed_field_names]

    def get_update_rules(self, update_fields: Iterable[str] | None, write_type: WriteType) -> RowWriteRules:
        """The rules of an UPDATE of an instance's row - ``Meta.optimistic_lock_field`` named among
        ``update_fields`` is the version the row is matched by, bumped as on every update.

        Args:
            update_fields: The fields to write, None for every writable one.
            write_type: What the update does.

        Returns:
            The rules.

        Raises:
            QueryError, FieldError: ``update_fields`` names a field the update can't set.
        """
        key = (None if update_fields is None else tuple(update_fields), write_type)
        if key == self.update_rules_key and self.update_rules is not None:
            return self.update_rules
        if key[0] is None:
            rules = RowWriteRules.get(self.model, write_type, None, SAVE_METHOD_NAME)
        else:
            version_field = self.model._meta.optimistic_lock_field
            rules = RowWriteRules.get(
                self.model, write_type, [name for name in key[0] if name != version_field], SAVE_METHOD_NAME
            )
        self.update_rules_key = key
        self.update_rules = rules
        return rules

    async def _execute_update_statement(
        self,
        instance: type[Model] | Model,
        effective_fields: list[str],
        old_version_value: Any,
        apply_active_tenant_scope_guard: bool,
        only_live_row: bool,
    ) -> int | None:
        """Runs the UPDATE of the instance's row and applies the values it returns.

        Args:
            instance: The instance to write.
            effective_fields: The fields the UPDATE sets.
            old_version_value: The ``Meta.optimistic_lock_field`` value the row is matched by, None
                without one.
            apply_active_tenant_scope_guard: Add the active-tenant condition.
            only_live_row: Write the row only while it isn't soft-deleted.

        Returns:
            The number of matched rows.
        """
        meta = self.model._meta
        optimistic_lock_field = meta.optimistic_lock_field
        expressions = {}
        for field in effective_fields:
            instance_field = getattr(instance, field)
            if isinstance(instance_field, Expression):
                expressions[field] = instance_field
        value_fields = (
            [field for field in effective_fields if field not in expressions] if expressions else effective_fields
        )
        values = self._get_update_values(instance, value_fields)

        values.extend(self._pk_where_values(instance))
        if optimistic_lock_field:
            values.append(
                self.connection.dialect.types.get_db_value(
                    meta.fields_map[optimistic_lock_field], old_version_value, instance
                )
            )
        tenant_value_count = 1
        if apply_active_tenant_scope_guard:
            tenant_value_count, tenant_guard_values = self._get_tenant_scope_guard()
            values.extend(tenant_guard_values)
        update_sql, parameter_layout = self.get_update_sql(
            effective_fields,
            expressions,
            apply_active_tenant_scope_guard,
            tenant_value_count,
            only_live_row,
        )
        if parameter_layout is not None:
            values = UpdateParameterSlot.bind(parameter_layout, values)
        if meta.sensitive_fields:
            # The written values, then the key's in the WHERE.
            values = self.hide_sensitive_values(
                values, [*value_fields, *meta.primary_key_attribute_names], parameter_layout
            )
        # Rows come back where get_update_sql() returns the fields the database computes again.
        returns_rows = bool(meta.recomputed_db_fields) and self.connection.features.supports_returning
        rows, returned_rows = await self.connection.execute(update_sql, values, returns_rows=returns_rows)
        if (rows is None or rows > 0) and (generated_fields := meta.recomputed_db_fields):
            if returned_rows:
                ReturnedValues.apply_row(
                    self.model,
                    self.connection.dialect.types,
                    cast("Model", instance),
                    returned_rows[0],
                    generated_fields,
                    connection=self.connection,
                )
            elif not self.connection.features.supports_returning:
                ReturnedValues.leave_unloaded(
                    self.model, (cast("Model", instance),), generated_fields, connection=self.connection
                )
        return rows

    def _get_update_values(self, instance: type[Model] | Model, field_names: list[str]) -> list[Any]:
        """The values an UPDATE of ``instance`` writes to fields.

        Args:
            instance: The instance written.
            field_names: The fields, every one holding a value rather than an expression.

        Returns:
            One value per field.
        """
        return BulkWriteBatches.serialize_instance(  # type: ignore[type-var]
            self.model, self.connection.dialect.types, instance, field_names
        )

    async def execute_delete(
        self, instance: type[Model] | Model, apply_active_tenant_scope_guard: bool = True
    ) -> int | None:
        """Deletes the instance's row.

        Args:
            instance: The instance to delete.
            apply_active_tenant_scope_guard: See ``get_update_sql``'s identical parameter -
                ``False`` for a cascade-discovered descendant.

        Returns:
            The number of matched rows - ``0`` for a row whose ``Meta.optimistic_lock_field`` changed
            since the instance read it; ``None`` when the database doesn't report how many rows a
            statement wrote (``Features.reports_written_row_count``).
        """
        capture_needs = self.model._meta.change_capture_needs
        if capture_needs is not None and self.captures_changes and capture_needs.captures(RowOperation.DELETE):
            return await WriteSteps.run_whole(
                self.connection,
                InstanceCapture.delete(
                    self,
                    cast("Model", instance),
                    capture_needs,
                    apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                ),
            )
        if apply_active_tenant_scope_guard:
            tenant_value_count, tenant_guard_values = self._get_tenant_scope_guard()
            values = self._deleted_row_where_values(instance) + tenant_guard_values
            query = self.delete_query if tenant_value_count == 1 else self._get_delete_query(tenant_value_count)
        else:
            values = self._deleted_row_where_values(instance)
            query = self.delete_query_without_tenant_scope
        if self.model._meta.sensitive_fields:
            values = self.hide_sensitive_values(values, list(self.model._meta.primary_key_attribute_names))
        return (await self.connection.execute(query, values, returns_rows=False))[0]

    def _get_delete_query(self, tenant_value_count: int) -> str:
        """The DELETE of one row guarded by a tenant scope of several values.

        Args:
            tenant_value_count: How many values the scope has.

        Returns:
            The statement.
        """
        statement_key = (self.statements_key, tenant_value_count)
        query: str | None = StatementPlans.instance_deletes.get_for_model(self.model, statement_key)
        if query is None:
            row_columns = self._deleted_row_db_columns()
            basequery = self.connection.query_class.from_(self.model._meta.basetable)
            builder = self._where_by_columns(basequery, row_columns)
            tenant_condition, _ = self._tenant_scope_where(
                self.model._meta.basetable, len(row_columns), tenant_value_count=tenant_value_count
            )
            if tenant_condition is not None:
                builder = builder.where(tenant_condition)
            query = str(builder.delete())
            StatementPlans.instance_deletes[(self.model, *statement_key)] = query
        return query


# Imported last: hare.query imports the writers.
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches  # noqa: E402
