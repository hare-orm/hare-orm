from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import (
    QueryError,
)
from hare.fields.base.database_default import DatabaseDefault
from hare.models.tenancy import Tenancy
from hare.models.write.expression_assignment import ExpressionAssignment
from hare.models.write.insert_statement import InsertStatement
from hare.models.write.instance_values import InstanceValues
from hare.models.write.returned_values import ReturnedValues
from hare.models.write.update_parameter_slot import UpdateParameterSlot
from hare.models.write.written_value_check import WrittenValueCheck
from hare.query.expressions import Expression, ExpressionContext
from hare.query.plans.statement_plans import StatementPlans
from hare.sql.functions.cast import Cast
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.terms.base.parameter import Parameter
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.sql import Table
    from hare.sql.terms.criteria.criterion import Criterion


class InstanceWriter:
    """Writes one model's instances on one connection: the INSERT and UPDATE of ``save()`` and the
    DELETE of ``delete()``. The statements are computed once per model and connection type.

    Args:
        model: The model.
        db: The connection.
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
        "_full_save_candidate_fields",
        "_auto_now_field_names",
    )

    def __init__(self, model: type[Model], db: DatabaseClient) -> None:
        self.model = model
        self.db: DatabaseClient = db
        # The dialect and the builder class are part of the key: two contexts may use one connection
        # name for different databases.
        key = (
            self.db.connection_name,
            self.db.dialect,
            self.model._meta.query_builder_class,
            self.model._meta.schema,
            self.model._meta.db_table,
        )
        #: The writer's key in ``StatementPlans.instance_writes`` - what its UPDATEs and DELETEs of
        #: other shapes are kept under too.
        self.statements_key = key
        statements = StatementPlans.instance_writes.get_for_model(self.model, key)
        if statements is None:
            insert_statement = InsertStatement(self.model, self.db)
            self.regular_columns = insert_statement.get_field_names(with_primary_key=False)
            self.insert_query = self._get_insert_sql(insert_statement, self.regular_columns, primary_key_given=False)
            self.regular_columns_all = self.regular_columns
            self.insert_query_all = self.insert_query
            if self.model._meta.generated_db_fields:
                self.regular_columns_all = insert_statement.get_field_names(with_primary_key=True)
                self.insert_query_all = self._get_insert_sql(
                    insert_statement, self.regular_columns_all, primary_key_given=True
                )

            basequery = self.db.query_class.from_(self.model._meta.basetable)
            pk_where_delete_builder = self._where_by_columns(basequery, self._pk_db_columns())
            # A cascade deletes a descendant it found through a real FK match - without the
            # active-tenant condition, which could differ from the descendant's tenant.
            self.delete_query_without_tenant_scope = str(pk_where_delete_builder.delete())
            tenant_condition, _ = self._tenant_scope_where(self.model._meta.basetable, len(self._pk_db_columns()))
            delete_builder = (
                pk_where_delete_builder.where(tenant_condition)
                if tenant_condition is not None
                else pk_where_delete_builder
            )
            self.delete_query = str(delete_builder.delete())
            # What a full save() writes: every field but the primary key, generated fields and
            # Meta.soft_delete_field - only delete()/restore() write that one.
            self._full_save_candidate_fields = tuple(
                field
                for field in self.model._meta.fields_db_projection
                if field not in self.model._meta.pk_attr_names
                and field != self.model._meta.soft_delete_field
                and not self.model._meta.fields_map[field].generated
            )
            self._auto_now_field_names = tuple(
                field_name
                for field_name, field_obj in self.model._meta.fields_map.items()
                if getattr(field_obj, "auto_now", False)
            )

            StatementPlans.instance_writes[(self.model, *key)] = tuple(
                getattr(self, name) for name in self.PER_MODEL_CACHED_ATTRIBUTES
            )
        else:
            for name, value in zip(self.PER_MODEL_CACHED_ATTRIBUTES, statements, strict=True):
                setattr(self, name, value)

    def _where_by_columns(self, builder: QueryBuilder, db_columns: Iterable[str]) -> QueryBuilder:
        """ANDs one `col = $N` condition per column, in `db_columns`' own order - a composite PK
        (`self._pk_db_columns()`) is the same shape as any other multi-column equality filter, one
        condition per column - the WHERE of `__init__`'s own delete_query.
        """
        table = self.model._meta.basetable
        for idx, db_column in enumerate(db_columns):
            builder = builder.where(table[db_column] == self.parameter(idx))
        return builder

    def parameter(self, pos: int) -> Parameter:
        return Parameter(idx=pos + 1)

    def _pk_db_columns(self) -> list[str]:
        """The DB column name(s) making up the PK, in ``pk_attr`` order - one for a single-column
        PK, one per column for a composite one."""
        if self.model._meta.has_composite_primary_key:
            return [self.model._meta.fields_db_projection[name] for name in self.model._meta.pk_attr]
        return [self.model._meta.db_pk_column]

    def _pk_where_values(self, instance: type[Model] | Model) -> list[Any]:
        """The bound value(s) for a WHERE-by-pk clause, in the same column order
        ``get_update_sql``/``delete_query`` build it in - one value for a single-column PK, one
        per column (in ``pk_attr`` order) for a composite one."""
        if self.model._meta.has_composite_primary_key:
            pk_values = cast("tuple[Any, ...]", instance.pk)
            return [
                self.db.dialect.types.get_db_value(self.model._meta.fields_map[pk_name], pk_value, instance)
                for pk_name, pk_value in zip(self.model._meta.pk_attr, pk_values, strict=True)
            ]
        return [self.db.dialect.types.get_db_value(self.model._meta.pk, instance.pk, instance)]

    def _tenant_scope_where(
        self,
        table: Table,
        next_idx: int,
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
            next_idx: The 0-based position of the first bound value.
            parameter_factory: Builds a placeholder term for a position; ``self.parameter`` by
                default.
            tenant_value_count: How many tenant values the scope has - 1 for one or none.

        Returns:
            The condition and the next free position; ``(None, next_idx)`` without
            ``Meta.tenant_field``.
        """
        tenant_field = self.model._meta.tenant_field
        if not tenant_field:
            return None, next_idx
        parameter = parameter_factory or self.parameter
        tenant_db_column = self.model._meta.fields_db_projection[tenant_field]
        if tenant_value_count > 1:
            placeholders = [parameter(next_idx + position) for position in range(tenant_value_count)]
            return table[tenant_db_column].isin(placeholders), next_idx + tenant_value_count
        second_param: Term = parameter(next_idx + 1)
        # A parameter used only in IS NULL gives the database no type to infer - the dialect names
        # the cast.
        sql_type = self.db.dialect.get_field_parameter_cast_type(self.model._meta.fields_map[tenant_field])
        if sql_type is not None:
            second_param = Cast(second_param, sql_type)
        condition = (table[tenant_db_column] == parameter(next_idx)) | second_param.isnull()
        return condition, next_idx + 2

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
        get_db_value = self.db.dialect.types.get_db_value
        db_values = [get_db_value(field, value, self.model) for value in tenant_values]
        if len(db_values) > 1:
            return len(db_values), db_values
        return 1, db_values * 2

    def _get_insert_values(self, instance: Model, columns: list[str]) -> list[Any]:
        """The values an INSERT of ``instance`` binds.

        Args:
            instance: The instance inserted.
            columns: The column list, one of this writer's own.

        Returns:
            One value per column.
        """
        # Deferred import: hare.query imports the executors.
        from hare.query.statements.write.bulk_write_batches import BulkWriteBatches

        return BulkWriteBatches.serialize_instance(self.model, self.db.dialect.types, instance, columns)

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

    async def _execute_insert_statement(self, query: str, values: list[Any]) -> Any:
        """Runs one INSERT.

        Args:
            query: The INSERT, with a RETURNING clause when the database generates columns.
            values: Its parameters.

        Returns:
            The row the INSERT returned; without one, the key the database gave the row where the
            driver reports it, else None.
        """
        result = await self.db.execute(query, values, returns_rows=True)
        rows = result[1]
        if rows:
            return rows[0]
        return result.inserted_id

    async def _process_insert_result(self, instance: Model, results: Any) -> None:
        """Sets the values an INSERT returned - the columns the database generated - on the
        instance.

        Args:
            instance: The inserted instance.
            results: The row the INSERT returned, the key the database gave the row (a database
                without ``RETURNING``), or None.
        """
        if results is None:
            return
        meta = self.model._meta
        if not hasattr(results, "keys"):
            # The key of a row inserted without RETURNING - set when the database generated it.
            pk_field = meta.pk
            pk_attr = cast("str", meta.pk_attr)
            if pk_field is not None and pk_field.generated and getattr(instance, pk_attr) is None:
                setattr(instance, pk_attr, self.db.dialect.types.get_python_value(pk_field, results))
            return
        ReturnedValues.apply_row(self.model, self.db.dialect.types, instance, results)

    async def _execute_insert_omitting_defaults(
        self, instance: Model, field_names: list[str], primary_key_given: bool
    ) -> Any:
        """Runs the INSERT of an instance leaving the fields holding ``DatabaseDefault`` to the
        database.

        Args:
            instance: The instance.
            field_names: The fields an INSERT writes.
            primary_key_given: The instance carries its primary key.

        Returns:
            What the INSERT returned (``_execute_insert_statement()``).
        """
        omitted_field_names = [
            field_name for field_name in field_names if isinstance(getattr(instance, field_name), DatabaseDefault)
        ]
        written_field_names = [field_name for field_name in field_names if field_name not in omitted_field_names]
        fields_map = self.model._meta.fields_map
        get_db_value = self.db.dialect.types.get_db_value
        values = [
            get_db_value(fields_map[field_name], getattr(instance, field_name), instance)
            for field_name in written_field_names
        ]
        sql = self._get_insert_sql(
            InsertStatement(self.model, self.db),
            written_field_names,
            primary_key_given=primary_key_given,
            omitted_field_names=omitted_field_names,
        )
        return await self._execute_insert_statement(sql, values)

    def _has_db_default_values(self, instance: Model, field_names: list[str]) -> bool:
        """Whether a field of the instance holds ``DatabaseDefault``."""
        if not self.model._meta.db_default_db_columns:
            return False
        for field_name in field_names:
            if isinstance(getattr(instance, field_name, None), DatabaseDefault):
                return True
        return False

    async def execute_insert(self, instance: Model) -> None:
        """Inserts the instance's row and sets on it what the database gave the row.

        Args:
            instance: The instance.
        """
        primary_key_given = instance._custom_generated_pk
        field_names, insert_sql = (
            (self.regular_columns_all, self.insert_query_all)
            if primary_key_given
            else (self.regular_columns, self.insert_query)
        )
        if self._has_db_default_values(instance, field_names):
            insert_result = await self._execute_insert_omitting_defaults(instance, field_names, primary_key_given)
            await self._process_insert_result(instance, insert_result)
            if not self.db.features.supports_returning:
                await self._fetch_db_defaults_after_insert(instance)
            return
        insert_result = await self._execute_insert_statement(
            insert_sql, self._get_insert_values(instance, field_names)
        )
        await self._process_insert_result(instance, insert_result)

    async def _fetch_db_defaults_after_insert(self, instance: Model) -> None:
        """Reads the values the database gave the ``db_default`` columns of a just-inserted row
        with a SELECT by primary key - on a database without RETURNING, after an INSERT that
        left them to the database.

        Args:
            instance: The inserted instance.
        """

        db_default_db_columns = self.model._meta.db_default_db_columns
        if not db_default_db_columns:
            return

        # Determine which fields still have DatabaseDefault (not populated by RETURNING)
        fields_to_fetch = []
        db_projection_reverse = self.model._meta.fields_db_projection_reverse
        for db_col in db_default_db_columns:
            model_field = db_projection_reverse.get(db_col, db_col)
            if isinstance(getattr(instance, model_field, None), DatabaseDefault):
                fields_to_fetch.append(db_col)

        if not fields_to_fetch:
            return

        # Need PK to SELECT - a composite pk is always a tuple of already-known values (never
        # auto-generated, see CompositePrimaryKey's own validation), so it's never None itself,
        # but any component could still be unset on a not-fully-constructed instance.
        if instance.pk is None or (isinstance(instance.pk, tuple) and None in instance.pk):
            return

        # Build SELECT via hare.sql for proper quoting
        table = self.model._meta.basetable
        query = self.db.query_class.from_(table).select(*fields_to_fetch)
        for idx, pk_db_column in enumerate(self._pk_db_columns()):
            query = query.where(table[pk_db_column] == self.parameter(idx))
        _, rows = await self.db.execute(str(query), self._pk_where_values(instance))

        if rows:
            row = rows[0]
            for db_col in fields_to_fetch:
                model_field = db_projection_reverse.get(db_col, db_col)
                field_object = self.model._meta.fields_map[model_field]
                raw_value = row[db_col]
                setattr(instance, model_field, self.db.dialect.types.get_python_value(field_object, raw_value))

    def get_update_sql(
        self,
        update_fields: Iterable[str] | None,
        expressions: dict[str, Expression] | None,
        apply_active_tenant_scope_guard: bool = True,
        expression_returning_columns: tuple[str, ...] = (),
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
            expression_returning_columns: The columns returned for ``WrittenValueCheck``.
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
        expressions = expressions or {}
        parameter: Callable[[int], Term] = UpdateParameterSlot.as_term if expressions else self.parameter
        table = self.model._meta.basetable
        query = self.db.query_class.update(table)
        parameter_idx = 0
        pk_attr_names = self.model._meta.pk_attr_names
        for field in fields_tuple or self.model._meta.fields_db_projection.keys():
            db_column = self.model._meta.fields_db_projection[field]
            field_object = self.model._meta.fields_map[field]
            if field_object.generated:
                if fields_tuple:
                    raise QueryError(f"Can't update generated field {field}")
                continue
            if field not in pk_attr_names:
                if field not in expressions.keys():
                    query = query.set(db_column, parameter(parameter_idx))
                    parameter_idx += 1
                else:
                    expression_term = ExpressionAssignment.get_term(
                        field,
                        field_object,
                        expressions[field],
                        ExpressionContext(
                            model=self.model,
                            dialect=self.db.dialect,
                            connection=self.db,
                            table=table,
                            annotations={},
                        ),
                    )
                    query = query.set(db_column, expression_term)

        if self.model._meta.has_composite_primary_key:
            for pk_name in self.model._meta.pk_attr:
                pk_db_column = self.model._meta.fields_db_projection[pk_name]
                query = query.where(table[pk_db_column] == parameter(parameter_idx))
                parameter_idx += 1
        else:
            query = query.where(table[self.model._meta.db_pk_column] == parameter(parameter_idx))
            parameter_idx += 1
        if optimistic_lock_field := self.model._meta.optimistic_lock_field:
            # Optimistic locking: only matches the row if it's still at the version this instance
            # was read at - execute_update binds the pre-increment value here, and the freshly
            # incremented value as this field's own SET (it's included in effective_fields there).
            version_db_column = self.model._meta.fields_db_projection[optimistic_lock_field]
            query = query.where(table[version_db_column] == parameter(parameter_idx))
            parameter_idx += 1

        if apply_active_tenant_scope_guard:
            tenant_condition, _ = self._tenant_scope_where(table, parameter_idx, parameter, tenant_value_count)
            if tenant_condition is not None:
                query = query.where(tenant_condition)
        if only_live_row:
            soft_delete_field = cast("str", self.model._meta.soft_delete_field)
            query = query.where(table[self.model._meta.fields_db_projection[soft_delete_field]].isnull())

        # A generated column is recomputed by the UPDATE - read back so the instance doesn't keep a
        # stale value. A generated primary key is never generated anew.
        if self.db.features.supports_returning and (generated_fields := self.model._meta.recomputed_db_fields):
            query = query.returning(*generated_fields)
        if expression_returning_columns:
            query = query.returning(*expression_returning_columns)

        if not expressions:
            sql = query.get_sql()
            StatementPlans.instance_updates[(self.model, *statement_key)] = sql
            return sql, None
        sql, parameter_layout = query.get_parameterized_sql()
        return sql, parameter_layout

    async def execute_update(
        self,
        instance: type[Model] | Model,
        update_fields: Iterable[str] | None,
        apply_active_tenant_scope_guard: bool = True,
        only_live_row: bool = False,
    ) -> int | None:
        """Updates the instance's row.

        Args:
            instance: The instance to write.
            update_fields: Fields to write, or ``None`` for every writable field.
            apply_active_tenant_scope_guard: Passed straight through to ``get_update_sql``
                (see its own docstring) - ``False`` for a cascade-discovered descendant, whose
                row a real FK match already verified.
            only_live_row: Write the row only while it isn't soft-deleted.

        Returns:
            The number of matched rows, or ``None`` when there was nothing to write.
        """
        effective_fields: list[str] = []
        if update_fields is not None:
            for field in update_fields:
                field_obj = self.model._meta.fields_map[field]
                if field in self.model._meta.pk_attr_names:
                    raise QueryError(f"Can't update pk field, use `{self.model.__name__}.objects.create()` instead.")
                if field_obj.generated:
                    raise QueryError(f"Can't update generated field {field}")
                instance_field = getattr(instance, field)
                if isinstance(instance_field, DatabaseDefault):
                    continue
                effective_fields.append(field)
        else:
            # pk/generated fields are already excluded from _full_save_candidate_fields (a pure
            # function of the model, precomputed once in __init__ - see its own comment) - only
            # the per-instance DatabaseDefault check still needs to run per call here.
            for field in self._full_save_candidate_fields:
                instance_field = getattr(instance, field)
                if isinstance(instance_field, DatabaseDefault):
                    continue
                effective_fields.append(field)

        if not effective_fields:
            return None

        # auto_now fields are written on every save(), whatever update_fields names.
        for field_name in self._auto_now_field_names:
            if field_name not in effective_fields:
                effective_fields.append(field_name)

        optimistic_lock_field = self.model._meta.optimistic_lock_field
        old_version_value = None
        model_instance = cast("Model", instance)
        # The auto_now stamps (to_db_value() below syncs them onto the instance) and the lock
        # bump change the instance before the outcome is known.
        changed_values = InstanceValues()
        try:
            changed_values.capture(
                (model_instance,), [name for name in self._auto_now_field_names if name in effective_fields]
            )
            if optimistic_lock_field:
                # Bumped on every update - the old value, bound as an extra WHERE parameter by
                # get_update_sql(), makes the UPDATE match only a row nothing changed since.
                old_version_value = changed_values.bump_optimistic_lock(model_instance, optimistic_lock_field)
                if optimistic_lock_field not in effective_fields:
                    effective_fields.append(optimistic_lock_field)

            expressions = {}
            value_fields = []
            written_value_check = WrittenValueCheck(self.model)
            for field in effective_fields:
                # An auto_now field left unloaded is still written - to_db_value() stamps it.
                instance_field = (
                    getattr(instance, field, None) if field in self._auto_now_field_names else getattr(instance, field)
                )
                if isinstance(instance_field, Expression):
                    expressions[field] = instance_field
                    written_value_check.add(
                        self.db.dialect,
                        self.model._meta.fields_db_projection[field],
                        self.model._meta.fields_map[field],
                    )
                else:
                    value_fields.append(field)
            values = self._get_update_values(instance, value_fields)

            values.extend(self._pk_where_values(instance))
            if optimistic_lock_field:
                values.append(
                    self.db.dialect.types.get_db_value(
                        self.model._meta.fields_map[optimistic_lock_field], old_version_value, instance
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
                tuple(written_value_check.get_returning_columns()) if written_value_check else (),
                tenant_value_count,
                only_live_row,
            )
            if parameter_layout is not None:
                values = UpdateParameterSlot.bind(parameter_layout, values)
            if written_value_check:
                rows, returned_rows = await written_value_check.execute(self.db, update_sql, values)
            else:
                rows, returned_rows = await self.db.execute(update_sql, values)
            if rows > 0 and (generated_fields := self.model._meta.recomputed_db_fields) and returned_rows:
                ReturnedValues.apply_row(
                    self.model, self.db.dialect.types, cast("Model", instance), returned_rows[0], generated_fields
                )
            if rows == 0:
                # Nothing matched (typically a concurrent version conflict) - the row never changed.
                changed_values.restore()
            else:
                changed_values.keep(self.db)
            return rows
        except BaseException:
            changed_values.restore()
            raise

    def _get_update_values(self, instance: type[Model] | Model, field_names: list[str]) -> list[Any]:
        """The values an UPDATE of ``instance`` writes to fields.

        Args:
            instance: The instance written.
            field_names: The fields, every one holding a value rather than an expression.

        Returns:
            One value per field.
        """
        instance_values = instance.__dict__
        if all(name in instance_values for name in field_names):
            # Deferred import: hare.query imports the executors.
            from hare.query.statements.write.bulk_write_batches import BulkWriteBatches

            return BulkWriteBatches.serialize_instance(
                self.model, self.db.dialect.types, cast("Model", instance), field_names
            )
        # An auto_now field left unloaded - its writer stamps None.
        fields_map = self.model._meta.fields_map
        get_db_value = self.db.dialect.types.get_db_value
        return [get_db_value(fields_map[name], getattr(instance, name, None), instance) for name in field_names]

    async def execute_delete(self, instance: type[Model] | Model, apply_active_tenant_scope_guard: bool = True) -> int:
        """Deletes the instance's row.

        Args:
            instance: The instance to delete.
            apply_active_tenant_scope_guard: See ``get_update_sql``'s identical parameter -
                ``False`` for a cascade-discovered descendant.

        Returns:
            The number of matched rows.
        """
        if apply_active_tenant_scope_guard:
            tenant_value_count, tenant_guard_values = self._get_tenant_scope_guard()
            values = self._pk_where_values(instance) + tenant_guard_values
            query = self.delete_query if tenant_value_count == 1 else self._get_delete_query(tenant_value_count)
        else:
            values = self._pk_where_values(instance)
            query = self.delete_query_without_tenant_scope
        return (await self.db.execute(query, values))[0]

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
            pk_columns = self._pk_db_columns()
            builder = self._where_by_columns(self.db.query_class.from_(self.model._meta.basetable), pk_columns)
            tenant_condition, _ = self._tenant_scope_where(
                self.model._meta.basetable, len(pk_columns), tenant_value_count=tenant_value_count
            )
            query = str(builder.where(tenant_condition).delete())
            StatementPlans.instance_deletes[(self.model, *statement_key)] = query
        return query
