from __future__ import annotations

from collections.abc import Awaitable, Collection, Generator, Iterable, Sequence
from itertools import repeat
from operator import attrgetter
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.exceptions import (
    FieldError,
    QueryError,
    UnSupportedError,
)
from hare.fields.base.database_default import DatabaseDefault
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.tenancy import Tenancy
from hare.models.write.insert_conflict import InsertConflict
from hare.models.write.insert_statement import InsertStatement
from hare.models.write.instance_values import InstanceValues
from hare.models.write.instance_writer import InstanceWriter
from hare.models.write.returned_values import ReturnedValues
from hare.models.write.write_fields import WriteFields
from hare.models.write.write_steps import WriteSteps
from hare.query.constants import UPSERT_INSERTED_FLAG_ALIAS
from hare.query.plans.statement_plans import StatementPlans
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.write.bulk_write_batches import BulkWriteBatches
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.terms.base.literal_value import LiteralValue
from hare.sql.terms.base.parameterizer import Parameterizer

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class BulkCreateQuery(AwaitableQuery[TModel], Generic[TModel]):
    plannable = False

    __slots__ = (
        "_objects",
        "_tenant_scoped",
        "_ignore_conflicts",
        "_batch_size",
        "_writer",
        "_reported_rows",
        "_existing_primary_keys",
        "_update_fields",
        "_on_conflict",
        "_on_conflict_constraint",
        "_conflict_where",
        "_returning",
        "_returning_explicitly_requested",
        "_use_copy",
        "_conflict_update_tenants",
        "_rows_committed_outside_transaction",
    )

    def __init__(
        self,
        model: type[TModel],
        db: DatabaseClient,
        objects: Iterable[TModel],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_fields: Iterable[str] | None = None,
        on_conflict: Iterable[str] | None = None,
        on_conflict_constraint: str | None = None,
        conflict_where: str | None = None,
        returning: bool = False,
        returning_explicitly_requested: bool = False,
        use_copy: bool = False,
        tenant_scoped: bool = False,
    ):
        super().__init__(model)
        self._objects = objects
        self._ignore_conflicts = ignore_conflicts
        self._batch_size = batch_size
        self._apply_db(db)
        self._update_fields = update_fields
        self._on_conflict = on_conflict
        self._on_conflict_constraint = on_conflict_constraint
        self._conflict_where = conflict_where
        self._returning = returning
        # Whether `returning=` was passed, not taken from Meta.returning - an explicit one a dialect
        # can't serve raises, an inherited one falls back to False.
        self._returning_explicitly_requested = returning_explicitly_requested
        self._use_copy = use_copy
        # Whether the rows belong to the tenant active when the query runs (Meta.tenant_field set,
        # no .all_tenants()).
        self._tenant_scoped = tenant_scoped
        # The tenant values an ON CONFLICT DO UPDATE is limited to - a conflicting row of another
        # tenant is left untouched instead of overwritten; None for no limit (every tenant).
        # Set when the query runs.
        self._conflict_update_tenants: tuple[Any, ...] | None = None
        # Built per execution, by _run()/sql(), once the connection is chosen.
        self._writer: InstanceWriter = None  # type: ignore[assignment]
        #: The rows the statements returned for the report of the changes - None when the
        #: report doesn't need them.
        self._reported_rows: list[dict[str, Any]] | None = None
        #: The primary keys of the objects' rows that existed before an upsert - read where the
        #: dialect has no inserted-row flag.
        self._existing_primary_keys: set[tuple[Any, ...]] | None = None
        # Objects whose rows a COPY committed on its own connection during the current execution.
        self._rows_committed_outside_transaction: list[TModel] = []

    @staticmethod
    def get_db_field_names(model: type[Model], names: Iterable[str], parameter_name: str) -> list[str]:
        """Maps the field names given to ``update_fields``/``on_conflict`` onto the model fields
        that own a column - a forward FK/O2O relation name becomes its key field(s) (``owner`` ->
        ``owner_id``), ``pk`` becomes the primary key field(s).

        Args:
            model: The model being inserted into.
            names: Field names as the caller passed them.
            parameter_name: Names the parameter in the raised message.

        Returns:
            Column-owning field names, deduplicated, in the given order.

        Raises:
            FieldError: A name is not a field with a column on this model's own table.
        """
        meta = model._meta
        db_field_names: list[str] = []
        for name in names:
            field_object = meta.fields_map.get(name)
            if name == "pk" and field_object is None:
                db_field_names.extend(meta.pk_attr_names)
            elif isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                db_field_names.extend(field_object.source_fields)
            elif name in meta.fields_db_projection:
                db_field_names.append(name)
            else:
                raise FieldError(
                    f'Unknown field "{name}" in bulk_create() {parameter_name} for model "{model.__name__}" - '
                    "expected a field with a column on its own table"
                )
        return list(dict.fromkeys(db_field_names))

    def _validate_dialect_dependent_options(self) -> None:
        """Checks on_conflict_constraint, conflict_where, returning and use_copy against the dialect of
        the connection the query runs on - known only once it is awaited.

        Raises:
            UnSupportedError: The dialect has no such option, or only the primary key can conflict
                on it. A ``returning`` inherited from ``Meta.returning`` falls back to False
                instead.
        """
        dialect = self._db.dialect
        if self._on_conflict_constraint and not dialect.supports_conflict_constraint_names:
            raise UnSupportedError(f"on_conflict_constraint is not supported by the {dialect} dialect")
        if self._conflict_where and not dialect.supports_conflict_where:
            raise UnSupportedError(f"conflict_where is not supported by the {dialect} dialect")
        if not dialect.supports_unique_constraints:
            self._validate_primary_key_conflict_target()
        if self._returning and not dialect.guarantees_returning_order and not self._objects_have_primary_keys():
            if not self._returning_explicitly_requested:
                self._returning = False
            else:
                raise UnSupportedError(
                    f"bulk_create(returning=True) on {dialect} needs every object's primary key set - "
                    "RETURNING's row order for a multi-row INSERT is not guaranteed there, so returned "
                    "rows are matched back to their objects by primary key."
                )
        if self._use_copy and not dialect.supports_copy:
            raise UnSupportedError(
                f"bulk_create(use_copy=True) is not supported on {dialect} - it has no COPY bulk-load protocol."
            )

    def _objects_have_primary_keys(self) -> bool:
        """Whether every object carries its primary key - a returned row is then matched to its
        object by key rather than by position.

        Returns:
            True when every key is set.
        """
        if not self.model._meta.has_primary_key:
            return False
        self._objects = list(self._objects)
        if self.model._meta.has_composite_primary_key:
            return all(None not in obj.pk for obj in self._objects)
        return all(obj.pk is not None for obj in self._objects)

    def _validate_primary_key_conflict_target(self) -> None:
        """Rejects a conflict target other than the primary key, on a database without unique
        constraints - the only rows that can conflict there share a primary key.

        Raises:
            UnSupportedError: ``on_conflict_constraint`` is given, or ``on_conflict`` names other
                columns than the primary key's.
        """
        dialect = self._db.dialect
        if self._on_conflict_constraint:
            raise UnSupportedError(
                f"on_conflict_constraint is not supported by the {dialect} dialect - it has no unique constraints"
            )
        if not self._on_conflict:
            return
        conflict_field_names = self.get_db_field_names(self.model, self._on_conflict, "on_conflict")
        if set(conflict_field_names) != set(self.model._meta.pk_attr_names):
            raise UnSupportedError(
                f"bulk_create(on_conflict={list(self._on_conflict)!r}) is not supported by the {dialect} "
                "dialect - it has no unique constraints, so only the primary key can conflict"
            )

    def _analyze_db_default_fields(self, columns: list[str]) -> set[str]:
        """The db_default fields every object leaves to the database - left out of the INSERT.

        Raises:
            QueryError: Some objects give a field a value and others leave it to its database
                default.
        """

        fields_map = self.model._meta.fields_map
        db_default_field_names = [fn for fn in columns if fields_map[fn].has_db_default()]
        if not db_default_field_names:
            return set()

        omit_fields: set[str] = set()
        for field_name in db_default_field_names:
            has_default = False
            has_value = False
            for instance in self._objects:
                if isinstance(getattr(instance, field_name), DatabaseDefault):
                    has_default = True
                else:
                    has_value = True
                if has_default and has_value:
                    raise QueryError(
                        f"Cannot use bulk_create() when field '{field_name}' has "
                        f"db_default and some instances provide explicit values while "
                        f"others rely on the database default. Either: "
                        f"(a) set the value explicitly on ALL instances, "
                        f"(b) omit it from ALL instances to use the database default, or "
                        f"(c) split into separate bulk_create() calls."
                    )
            if has_default and not has_value:
                omit_fields.add(field_name)

        return omit_fields

    def _get_insert_statement(self) -> InsertStatement:
        """The builder of this call's INSERTs, on the connection it runs on."""
        return InsertStatement(self.model, self._db)

    def _get_returning_columns(self, omit_fields: Collection[str] = ()) -> list[str]:
        """The columns a ``returning=True`` INSERT returns: the primary key, the generated
        columns, the fields left to their database default and, for an upsert, the version an
        update bumps."""
        return self._get_insert_statement().get_returning_columns(
            primary_key_given=False,
            omitted_field_names=omit_fields,
            returns_primary_key=True,
            returns_version=bool(self._update_fields),
        )

    def _get_returned_field_names(self, omit_fields: set[str]) -> list[str]:
        """Field names a RETURNING row of this call can set on an object.

        Args:
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.

        Returns:
            The field names, deduplicated.
        """
        meta = self.model._meta
        returned_columns = self._get_returning_columns(omit_fields)
        if self._ignore_conflicts and self._on_conflict:
            returned_columns += [meta.fields_db_projection[name] for name in self._on_conflict]
        return list(dict.fromkeys(meta.fields_db_projection_reverse[column] for column in returned_columns))

    def _build_default_values_sql(self, omit_fields: set[str] | None = None) -> str:
        """The ``DEFAULT VALUES`` INSERT of one row - every column left to the database - returning
        its columns when ``returning=True``.

        Args:
            omit_fields: Fields left out because every object relies on its database default.

        Returns:
            The SQL.
        """
        returning = self._get_returning_columns(omit_fields or ()) if self._returning else ()
        return self._get_row_template(None, returning)[0]

    def _get_row_template(self, columns: list[str] | None, returning: Sequence[str] = ()) -> tuple[str, list[Any]]:
        """The single-row INSERT template of this call's ``ON CONFLICT`` clause, kept in
        ``StatementPlans.insert_templates``.

        Args:
            columns: The row's columns; None for the ``DEFAULT VALUES`` INSERT.
            returning: The columns the INSERT returns.

        Returns:
            The SQL text and the values of the parameters following the row's.
        """
        conflict = None if columns is None else self._get_conflict()
        meta = self.model._meta
        key = (
            self.model,
            self._db.connection_name,
            self._db.dialect,
            meta.query_builder_class,
            meta.schema,
            meta.db_table,
            None if columns is None else tuple(columns),
            conflict,
            tuple(returning),
        )
        cacheable = True
        try:
            template = StatementPlans.insert_templates.get(key)
        except TypeError:
            # A tenant value the conflict update is limited to that can't be hashed - built each time.
            cacheable = False
            template = None
        if template is None:
            insert_statement = self._get_insert_statement()
            if columns is None:
                template = (str(insert_statement.get_query([], [], returning=returning)), [])
            else:
                query = insert_statement.get_query(
                    columns, [insert_statement.get_parameter_row(len(columns))], conflict=conflict, returning=returning
                )
                template = self._get_row_template_sql(query, len(columns))
            if cacheable:
                StatementPlans.insert_templates[key] = template
        return template

    def _filter_columns(self, omit_fields: set[str], include_generated: bool = False) -> list[str]:
        """The columns of ``_filtered_field_names()``."""
        return self._get_insert_statement().get_columns(self._filtered_field_names(omit_fields, include_generated))

    def _get_conflict(self) -> InsertConflict | None:
        """What a conflicting row does - None without ``ignore_conflicts``/``update_fields``."""
        if not (self._update_fields or self._ignore_conflicts):
            return None
        return InsertConflict(
            target_field_names=tuple(self._on_conflict or ()),
            constraint_name=self._on_conflict_constraint,
            condition_sql=self._conflict_where,
            update_field_names=tuple(self._update_fields or ()),
            update_tenants=self._conflict_update_tenants,
        )

    @property
    def _may_skip_conflicting_rows(self) -> bool:
        """Whether an ``ON CONFLICT`` clause can leave some objects' rows unwritten - ``DO
        NOTHING``, or a ``DO UPDATE`` limited to the tenant scope's rows."""
        return self._ignore_conflicts or (bool(self._update_fields) and self._conflict_update_tenants is not None)

    def _filtered_field_names(self, omit_fields: set[str], include_generated: bool = False) -> list[str]:
        """The fields an INSERT of this call writes.

        Args:
            omit_fields: Fields left to their database default.
            include_generated: The objects carry their primary key.

        Returns:
            The field names.
        """
        return self._get_insert_statement().get_field_names(
            with_primary_key=include_generated, omitted_field_names=omit_fields
        )

    def _build_inline_insert_sql(
        self,
        field_names: list[str],
        db_columns: list[str],
        objects: list[TModel],
        omit_fields: set[str],
    ) -> str:
        """The INSERT of ``objects`` with every value rendered into the SQL - one multi-row statement
        per batch, for ``sql(params_inline=True)``.
        """
        if not objects:
            return ""
        if not db_columns:
            return ";".join([self._build_default_values_sql(omit_fields)] * len(objects))

        fields_map = self.model._meta.fields_map
        insert_statement = self._get_insert_statement()
        conflict = self._get_conflict()
        statements = []
        for objects_item in BulkWriteBatches.get_batches(objects, self._batch_size):
            if not objects_item:
                continue
            rows = [
                [self.dialect.types.get_db_value(fields_map[fn], getattr(obj, fn), obj) for fn in field_names]
                for obj in objects_item
            ]
            statements.append(str(insert_statement.get_query(db_columns, rows, conflict=conflict)))
        return ";".join(statements)

    def _make_inline_statements(self, omit_fields: set[str]) -> list[str]:
        """The INSERT statements with every value rendered in - one per group of objects (without
        and with a caller-given primary key)."""
        custom_pk_objects = [o for o in self._objects if o._custom_generated_pk]
        regular_objects = [o for o in self._objects if not o._custom_generated_pk]

        statements = []
        if regular_objects:
            statements.append(
                self._build_inline_insert_sql(
                    self._filtered_field_names(omit_fields),
                    self._filter_columns(omit_fields),
                    regular_objects,
                    omit_fields,
                )
            )
        if custom_pk_objects:
            statements.append(
                self._build_inline_insert_sql(
                    self._filtered_field_names(omit_fields, include_generated=True),
                    self._filter_columns(omit_fields, include_generated=True),
                    custom_pk_objects,
                    omit_fields,
                )
            )
        return [statement for statement in statements if statement]

    def _make_queries(self, omit_fields: set[str] | None = None) -> tuple[str, str]:
        (insert_sql, _), (insert_sql_all, _) = self._make_template_statements(omit_fields)
        return insert_sql, insert_sql_all

    def _make_template_statements(
        self, omit_fields: set[str] | None = None
    ) -> tuple[tuple[str, list[Any]], tuple[str, list[Any]]]:
        """The single-row INSERT templates of objects without and with a caller-given primary
        key, each with the values of the parameters following the row's own.

        Args:
            omit_fields: Fields left to their database default.

        Returns:
            ``(sql, values)`` of both templates.
        """
        if omit_fields is None:
            omit_fields = set()
        conflict = self._get_conflict()
        if conflict is None and not omit_fields:
            return (self._writer.insert_query, []), (self._writer.insert_query_all, [])

        insert_statement = self._get_insert_statement()
        templates: list[tuple[str, list[Any]]] = []
        for primary_key_given in (False, True):
            columns = self._filter_columns(omit_fields, include_generated=primary_key_given)
            if not columns:
                default_sql = self._build_default_values_sql(omit_fields)
                return (default_sql, []), (default_sql, [])
            templates.append(
                self._get_row_template(
                    columns, insert_statement.get_returning_columns(primary_key_given=primary_key_given)
                )
            )
        return templates[0], templates[1]

    @staticmethod
    def _get_row_template_sql(query: QueryBuilder, row_parameter_count: int) -> tuple[str, list[Any]]:
        """Renders a single-row INSERT template, binding the values of its ``ON CONFLICT`` clause as
        parameters numbered after the row's own placeholders.

        Args:
            query: The INSERT query, its row values already placeholders.
            row_parameter_count: How many placeholders the row takes.

        Returns:
            The SQL text and the values of the parameters following the row's.
        """
        parameterizer = Parameterizer()
        parameterizer.values.extend([None] * row_parameter_count)
        sql = query.get_sql(query.QUERY_CLS.SQL_CONTEXT.copy(parameterizer=parameterizer))
        return sql, parameterizer.values[row_parameter_count:]

    def _serialize_instances(self, instances: list[TModel], columns: list[str]) -> list[list[Any]]:
        return BulkWriteBatches.serialize_instances(self.model, self._db.dialect.types, instances, columns)

    async def _execute_parameterized_inserts(
        self,
        db_columns: list[str],
        field_names: list[str],
        objects: list[TModel],
        omit_fields: set[str],
        populate_returned_fields: bool = False,
    ) -> None:
        """Inserts the objects by one of two strategies: a single-row INSERT run through
        ``execute_many()`` per chunk, or - where the driver's ``execute_many()`` scales poorly, or
        rows are read back - one multi-row INSERT per chunk. RETURNING is added only with
        ``populate_returned_fields``.
        """
        if not objects or not db_columns:
            return
        if populate_returned_fields or self._reports_returned_rows or self._db.features.execute_many_scales_poorly:
            await self._execute_via_multi_row_statement(
                db_columns, field_names, objects, omit_fields, populate_returned_fields=populate_returned_fields
            )
        else:
            await self._execute_via_execute_many(db_columns, field_names, objects, omit_fields)

    async def _execute_via_execute_many(
        self,
        db_columns: list[str],
        field_names: list[str],
        objects: list[TModel],
        omit_fields: set[str],
    ) -> None:
        sql, statement_values = self._get_row_template(db_columns)
        for objects_item in BulkWriteBatches.get_batches(objects, self._batch_size):
            objects_item = list(objects_item)
            if not objects_item:
                continue
            rows = self._serialize_instances(objects_item, field_names)
            if statement_values:
                rows = [[*row, *statement_values] for row in rows]
            await self._db.execute_many(sql, rows)

    async def _execute_via_multi_row_statement(
        self,
        db_columns: list[str],
        field_names: list[str],
        objects: list[TModel],
        omit_fields: set[str],
        populate_returned_fields: bool = False,
    ) -> None:
        batch_size = self._get_multi_row_batch_size(db_columns, omit_fields)
        returning_columns = self._get_returning_columns(omit_fields) if populate_returned_fields else []
        # ON CONFLICT may skip rows, so the returned rows no longer line up with the objects - the
        # conflict target's columns are returned too, to match them by value.
        conflict_field_names = (
            list(self._on_conflict) if (self._may_skip_conflicting_rows and self._on_conflict) else []
        )
        if populate_returned_fields and conflict_field_names:
            conflict_source_columns = [self.model._meta.fields_db_projection[fn] for fn in conflict_field_names]
            returning_columns = list(dict.fromkeys([*returning_columns, *conflict_source_columns]))
        statement_objects = [
            statement_objects_item
            for objects_item in BulkWriteBatches.get_batches(objects, batch_size)
            for statement_objects_item in self._split_by_conflict_key(list(objects_item))
        ]
        for objects_item in statement_objects:
            if not objects_item:
                continue
            rows = self._serialize_instances(objects_item, field_names)
            returns_rows = populate_returned_fields or self._reports_returned_rows
            sql, conflict_values = self._get_multi_row_template(
                db_columns, len(rows), returning_columns if returns_rows else None
            )
            values = [value for row in rows for value in row]
            values.extend(conflict_values)
            __, returned_rows = await self._db.execute(sql, values, returns_rows=returns_rows)
            if populate_returned_fields:
                self._populate_returned_fields_from_returning_rows(objects_item, returned_rows, conflict_field_names)
            if self._reported_rows is not None:
                self._reported_rows.extend(dict(row) for row in returned_rows)

    def _get_multi_row_template(
        self, db_columns: list[str], row_count: int, returning_columns: list[str] | None
    ) -> tuple[str, list[Any]]:
        """The multi-row INSERT of this call's ``ON CONFLICT`` clause for ``row_count`` rows, kept in
        ``StatementPlans.insert_templates``.

        Args:
            db_columns: Each row's columns.
            row_count: How many rows the INSERT writes.
            returning_columns: The columns it returns - with the terms the change report reads -
                None for no ``RETURNING``.

        Returns:
            The SQL text and the values of the parameters following the rows'.
        """
        conflict = self._get_conflict()
        meta = self.model._meta
        key = (
            self.model,
            self._db.connection_name,
            self._db.dialect,
            meta.query_builder_class,
            meta.schema,
            meta.db_table,
            "rows",
            tuple(db_columns),
            row_count,
            conflict,
            None if returning_columns is None else (tuple(returning_columns), self._reports_returned_rows),
        )
        cacheable = True
        try:
            template = StatementPlans.insert_templates.get(key)
        except TypeError:
            # A tenant value the conflict update is limited to that can't be hashed - built each time.
            cacheable = False
            template = None
        if template is None:
            # A row's placeholders are one string in one LiteralValue term, not a Parameter per value.
            dialect = self._db.dialect
            column_count = len(db_columns)
            param_rows = [
                [
                    LiteralValue(
                        ",".join(
                            dialect.get_placeholder(index)
                            for index in range(1 + row_index * column_count, 1 + (row_index + 1) * column_count)
                        )
                    )
                ]
                for row_index in range(row_count)
            ]
            query = self._get_insert_statement().get_query(
                db_columns,
                param_rows,
                conflict=conflict,
                returning=(
                    () if returning_columns is None else [*returning_columns, *self._get_reported_returning_terms()]
                ),
            )
            template = self._get_row_template_sql(query, row_count * column_count)
            if cacheable:
                StatementPlans.insert_templates[key] = template
        return template

    def _get_multi_row_batch_size(self, db_columns: list[str], omit_fields: set[str]) -> int:
        """Objects per multi-row ``INSERT``, kept under the backend's bind-parameter ceiling
        together with the parameters the statement binds besides its rows.

        Args:
            db_columns: The INSERT's columns.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.

        Returns:
            The batch size.
        """
        max_bind_params = self._db.features.max_bind_parameters - self._get_fixed_parameter_count(
            db_columns, omit_fields
        )
        return BulkWriteBatches.get_bind_param_safe_batch_size(self._batch_size, len(db_columns), max_bind_params)

    def _get_fixed_parameter_count(self, db_columns: list[str], omit_fields: set[str]) -> int:
        """Bind parameters one multi-row ``INSERT`` of this call carries besides its rows' values -
        those of its ``ON CONFLICT ... DO UPDATE`` clause (the tenant scope, the version bump).

        Args:
            db_columns: The INSERT's columns.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.

        Returns:
            The parameter count.
        """
        if not self._update_fields:
            return 0
        probe_row = [LiteralValue(",".join("NULL" for _ in db_columns))]
        probe_query = self._get_insert_statement().get_query(db_columns, [probe_row], conflict=self._get_conflict())
        parameterizer = Parameterizer()
        probe_query.get_sql(probe_query.QUERY_CLS.SQL_CONTEXT.copy(parameterizer=parameterizer))
        return len(parameterizer.values)

    def _get_conflict_update_key_field_names(self) -> list[str]:
        """The conflict target's field names of an ``ON CONFLICT ... DO UPDATE``.

        Returns:
            The field names, empty without ``update_fields`` or when a named constraint isn't
            one of the model's own ``UniqueConstraint``s.
        """
        if not self._update_fields:
            return []
        if self._on_conflict:
            return list(self._on_conflict)
        for constraint in self.model._meta.constraints:
            if isinstance(constraint, UniqueConstraint) and constraint.name == self._on_conflict_constraint:
                return self.get_db_field_names(self.model, constraint.fields, "on_conflict_constraint")
        return []

    def _split_by_conflict_key(self, objects: list[TModel]) -> list[list[TModel]]:
        """Splits ``objects`` into consecutive groups that never repeat a conflict key - one ``ON
        CONFLICT DO UPDATE`` can't update a row twice, so a later object with the same key goes into
        the next statement and its values win.

        Args:
            objects: The objects of one multi-row INSERT, in order.

        Returns:
            The groups, in order.
        """
        key_field_names = self._get_conflict_update_key_field_names()
        if not key_field_names:
            return [objects]
        groups: list[list[TModel]] = [[]]
        seen_keys: set[Any] = set()
        for obj in objects:
            key: Any = tuple(getattr(obj, field_name, None) for field_name in key_field_names)
            if any(value is None for value in key):
                # A NULL never conflicts with anything.
                key = None
            else:
                try:
                    hash(key)
                except TypeError:
                    key = repr(key)
            if key is not None and key in seen_keys:
                groups.append([])
                seen_keys = set()
            groups[-1].append(obj)
            if key is not None:
                seen_keys.add(key)
        return groups

    def _populate_returned_fields_from_returning_rows(
        self,
        objects_item: list[TModel],
        returned_rows: Sequence[Any],
        conflict_field_names: Sequence[str] = (),
    ) -> None:
        """Sets each object's database-computed columns from its ``RETURNING`` row - by position, by
        primary key where the database returns rows in no order, or by the conflict target's values
        when rows were skipped.

        Args:
            objects_item: The objects of one statement, in order.
            returned_rows: Its returned rows.
            conflict_field_names: The ``on_conflict=[...]`` fields, when rows can be skipped.

        Raises:
            QueryError: Rows were skipped and there is no conflict target to match the rest by.
        """
        types = self.dialect.types
        matches: Iterable[tuple[TModel, Any]]
        if len(returned_rows) != len(objects_item):
            if not conflict_field_names:
                raise QueryError(
                    f"bulk_create() on {self.model.__name__}: {len(objects_item) - len(returned_rows)} "
                    f"of {len(objects_item)} object(s) were skipped by ON CONFLICT DO NOTHING, and "
                    "returning=True cannot match the surviving rows back to their source objects "
                    "without an explicit on_conflict=[...] column list to match by value."
                )
            # Several objects sharing a conflict key: the first is inserted, the rest conflict
            # with it - the first owns the row.
            matches = ReturnedValues.match_rows(self.model, types, objects_item, returned_rows, conflict_field_names)
        elif not self.dialect.guarantees_returning_order:
            matches = ReturnedValues.match_rows(
                self.model, types, objects_item, returned_rows, self.model._meta.pk_attr_names
            )
        else:
            matches = zip(objects_item, returned_rows, strict=True)
        for obj, row in matches:
            self._apply_returning_row(obj, row)

    def _apply_returning_row(self, obj: TModel, row: Any) -> None:
        """Marks ``obj`` saved and sets every field ``row`` carries a column for.

        Args:
            obj: The object whose row was written.
            row: Its RETURNING row, keyed by DB column.
        """
        custom_generated_pk = obj._custom_generated_pk
        object.__setattr__(obj, "_saved_in_db", True)
        obj._remember_db(self._db)
        ReturnedValues.apply_row(self.model, self.dialect.types, obj, row)
        # Assigning the pk on a saved instance clears the flag - a caller-supplied pk stays marked so.
        object.__setattr__(obj, "_custom_generated_pk", custom_generated_pk)

    async def _execute_many(
        self,
        insert_sql: str,
        insert_sql_all: str,
        effective_columns: list[str],
        effective_columns_all: list[str],
        omit_fields: set[str],
    ) -> None:
        # Two groups, each with its own column list: objects carrying a primary key write it.
        custom_pk_instances = [o for o in self._objects if o._custom_generated_pk]
        regular_instances = [o for o in self._objects if not o._custom_generated_pk]

        if effective_columns_all:
            await self._execute_parameterized_inserts(
                self._filter_columns(omit_fields, include_generated=True),
                effective_columns_all,
                custom_pk_instances,
                omit_fields,
                populate_returned_fields=self._returning,
            )
        elif custom_pk_instances:
            # When all columns are omitted, there's no VALUES row to parameterize at all.
            await self._insert_default_rows(insert_sql_all, custom_pk_instances, omit_fields, False)

        if effective_columns:
            await self._execute_parameterized_inserts(
                self._filter_columns(omit_fields),
                effective_columns,
                regular_instances,
                omit_fields,
                populate_returned_fields=self._returning,
            )
        elif regular_instances:
            # All columns omitted (every non-generated column relies on its DB default) - no
            # VALUES row to parameterize at all.
            await self._insert_default_rows(insert_sql, regular_instances, omit_fields, self._returning)

    async def _insert_default_rows(
        self, insert_sql: str, objects: list[TModel], omit_fields: set[str], populate_returned_fields: bool
    ) -> None:
        """Inserts one all-defaults row per object - a single ``INSERT ... SELECT FROM
        generate_series(...)`` on Postgres, one ``DEFAULT VALUES`` statement per object on SQLite,
        which has no multi-row form of it.

        Args:
            insert_sql: The single-row ``DEFAULT VALUES`` statement.
            objects: The objects to insert.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.
            populate_returned_fields: Set each object's returned columns from its RETURNING row.
        """
        if not objects:
            return
        if (default_rows_source_sql := self._db.dialect.get_default_rows_source_sql(len(objects))) is not None:
            _, returned_rows = await self._db.execute(
                self._build_default_rows_sql(default_rows_source_sql, omit_fields, populate_returned_fields), []
            )
            if populate_returned_fields:
                self._populate_returned_fields_from_returning_rows(objects, returned_rows)
            return
        for obj in objects:
            # insert_sql (self._writer.insert_query) already carries its own RETURNING clause
            # for every generated_db_fields column (pk included, when auto-generated).
            returned_rows = (await self._db.execute(insert_sql, [], returns_rows=True)).rows
            if populate_returned_fields and returned_rows:
                self._populate_returned_fields_from_returning_rows([obj], [returned_rows[0]])

    def _build_default_rows_sql(self, rows_source_sql: str, omit_fields: set[str], with_returning: bool) -> str:
        """``INSERT INTO t <rows_source_sql>`` - rows of column defaults in one statement.

        Args:
            rows_source_sql: The dialect's source of the rows.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.
            with_returning: Add a RETURNING clause for the pk, generated and omitted columns.

        Returns:
            The SQL text.
        """
        query_class = self._db.query_class
        sql_context = query_class.SQL_CONTEXT
        returning_columns = self._get_returning_columns(omit_fields) if with_returning else []
        return query_class.get_insert_rows_source_sql(
            self.model._meta.basetable.get_sql(sql_context),
            rows_source_sql,
            [sql_context.quote(column) for column in returning_columns],
        )

    def _get_copy_column_types(self, field_names: list[str]) -> list[str]:
        """Each field's column type without its size ("VARCHAR(255)" -> "VARCHAR") - COPY needs the
        types declared.

        Args:
            field_names: The fields being copied, in column order.

        Returns:
            One type name per field.

        Raises:
            UnSupportedError: A field's type isn't supported by COPY.
        """
        column_types = [
            str(self.model._meta.fields_map[fn].get_column_type(self._db.dialect)).split("(")[0].strip()
            for fn in field_names
        ]
        for field_name, column_type in zip(field_names, column_types, strict=True):
            if not self._db.dialect.supports_copy_column_type(column_type):
                raise UnSupportedError(
                    f"bulk_create(use_copy=True) does not support field '{field_name}' "
                    f"(column type {column_type!r}) - the {self._db.dialect.name} COPY protocol doesn't "
                    "load that type; use the default multi-row INSERT path (use_copy=False) for this "
                    "model instead."
                )
        return column_types

    def _check_copy_supported_types(self, omit_fields: set[str]) -> None:
        """Checks the column types of every group ``_execute_via_copy`` loads before any is loaded -
        the two groups copy separately.

        Args:
            omit_fields: Fields left out of the INSERT because every object relies on its DB
                default.

        Raises:
            UnSupportedError: A field's type isn't supported by COPY.
        """
        has_custom_pk_objects = any(obj._custom_generated_pk for obj in self._objects)
        has_regular_objects = any(not obj._custom_generated_pk for obj in self._objects)
        if has_custom_pk_objects:
            self._get_copy_column_types(self._filtered_field_names(omit_fields, include_generated=True))
        if has_regular_objects:
            self._get_copy_column_types(self._filtered_field_names(omit_fields))

    def _runs_multiple_statements(
        self, effective_columns: list[str], effective_columns_all: list[str], omit_fields: set[str]
    ) -> bool:
        """Whether writing ``self._objects`` takes more than one statement (several batches,
        both the custom-pk and regular groups, or one DEFAULT VALUES statement per object) - the
        cases that need a transaction around them to stay all-or-nothing.

        Args:
            effective_columns: Column names of the regular group's INSERT.
            effective_columns_all: Column names of the custom-pk group's INSERT.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.

        Returns:
            True if more than one statement will run.
        """
        objects = list(self._objects)
        custom_pk_count = sum(map(attrgetter("_custom_generated_pk"), objects))
        uses_multi_row_statements = not self._use_copy and (
            self._db.features.execute_many_scales_poorly or self._returning
        )
        statement_count = 0
        for group_size, columns in (
            (custom_pk_count, effective_columns_all),
            (len(objects) - custom_pk_count, effective_columns),
        ):
            if not group_size:
                continue
            if not columns:
                statement_count += (
                    1 if self._db.dialect.get_default_rows_source_sql(group_size) is not None else group_size
                )
                continue
            batch_size = self._batch_size
            if uses_multi_row_statements:
                batch_size = self._get_multi_row_batch_size(columns, omit_fields)
            statement_count += 1 if batch_size is None else -(-group_size // max(1, batch_size))
        if statement_count == 1 and uses_multi_row_statements and len(self._split_by_conflict_key(objects)) > 1:
            return True
        return statement_count > 1

    async def _execute_writes(
        self,
        insert_sql: str,
        insert_sql_all: str,
        effective_columns: list[str],
        effective_columns_all: list[str],
        omit_fields: set[str],
    ) -> None:
        """Runs every INSERT/COPY statement for ``self._objects`` through whichever strategy
        ``use_copy`` selects.

        Args:
            insert_sql: DEFAULT VALUES/INSERT statement for the regular group.
            insert_sql_all: Same, for the custom-pk group.
            effective_columns: Column names of the regular group's INSERT.
            effective_columns_all: Column names of the custom-pk group's INSERT.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.
        """
        if self._use_copy:
            await self._execute_via_copy(
                insert_sql, insert_sql_all, effective_columns, effective_columns_all, omit_fields
            )
        else:
            await self._execute_many(insert_sql, insert_sql_all, effective_columns, effective_columns_all, omit_fields)

    async def _copy_objects(self, db_columns: list[str], field_names: list[str], objects: list[TModel]) -> None:
        """Loads ``objects`` through the dialect's COPY protocol, in batches of ``batch_size`` - COPY
        has no bind-parameter ceiling.
        """
        if not objects or not db_columns:
            return
        table = self.model._meta.db_table
        column_types = self._get_copy_column_types(field_names)
        for objects_item in BulkWriteBatches.get_batches(objects, self._batch_size):
            objects_item = list(objects_item)
            if not objects_item:
                continue
            rows = self._serialize_instances(objects_item, field_names)
            await self._db.copy(table, db_columns, [tuple(row) for row in rows], column_types)
            self._record_rows_committed_outside_transaction(objects_item)

    async def _execute_via_copy(
        self,
        insert_sql: str,
        insert_sql_all: str,
        effective_columns: list[str],
        effective_columns_all: list[str],
        omit_fields: set[str],
    ) -> None:
        """The ``use_copy=True`` counterpart of ``_execute_many()``: the same two groups, loaded
        through ``_copy_objects()``; a group with no column to write falls back to DEFAULT VALUES
        rows.
        """
        custom_pk_instances = [o for o in self._objects if o._custom_generated_pk]
        regular_instances = [o for o in self._objects if not o._custom_generated_pk]

        if effective_columns_all:
            await self._copy_objects(
                self._filter_columns(omit_fields, include_generated=True),
                effective_columns_all,
                custom_pk_instances,
            )
        elif custom_pk_instances:
            await self._insert_default_rows(insert_sql_all, custom_pk_instances, omit_fields, False)
            self._record_rows_committed_outside_transaction(custom_pk_instances)

        if effective_columns:
            await self._copy_objects(
                self._filter_columns(omit_fields),
                effective_columns,
                regular_instances,
            )
        elif regular_instances:
            await self._insert_default_rows(insert_sql, regular_instances, omit_fields, False)
            self._record_rows_committed_outside_transaction(regular_instances)

    def _record_rows_committed_outside_transaction(self, objects: list[TModel]) -> None:
        """Remembers ``objects`` as written for good when this backend's COPY commits on its own
        connection - a later failure of the same call leaves their rows in place, so they stay
        marked saved.

        Args:
            objects: Objects whose rows were just written.
        """
        if not self._db.copy_joins_transaction:
            self._rows_committed_outside_transaction.extend(objects)

    def _check_copy_groups_supported(self) -> None:
        """Rejects ``use_copy=True`` for objects both with and without an explicit primary key on
        a backend whose COPY can't join a transaction - the two groups load through two separate
        COPY statements, and the first one would stay written if the second failed.

        Raises:
            UnSupportedError: Both groups are present and COPY can't join a transaction.
        """
        if self._db.copy_joins_transaction:
            return
        has_custom_pk_objects = any(obj._custom_generated_pk for obj in self._objects)
        has_regular_objects = any(not obj._custom_generated_pk for obj in self._objects)
        if has_custom_pk_objects and has_regular_objects:
            raise UnSupportedError(
                f"bulk_create(use_copy=True) on {self.model.__name__} got objects both with and without an "
                "explicit primary key - they load through two separate COPY statements, and this backend's "
                "COPY can't run inside a transaction to keep them all-or-nothing. Split them into two "
                "bulk_create() calls, or use the default INSERT path (use_copy=False)."
            )

    def __await__(self) -> Generator[Any]:
        query = self._get_execution_query(True)
        query._validate_dialect_dependent_options()
        return query._report_bulk_create(query._run()).__await__()

    @property
    def _reports_returned_rows(self) -> bool:
        """Whether the statements return the rows they wrote for the report of the changes -
        when a listener hears the model and conflicting rows are skipped or updated, so not every
        object is a row inserted."""
        return self._reported_rows is not None

    def _get_reported_returning_terms(self) -> list[Any]:
        """The ``RETURNING`` terms the report of the changes reads: the primary key, and for an
        upsert the dialect's inserted-row flag - or the conflict key, matched against the keys
        read before the write.

        Returns:
            The terms.
        """
        if not self._reports_returned_rows:
            return []
        meta = self.model._meta
        terms: list[Any] = [meta.fields_db_projection[name] for name in meta.pk_attr_names]
        if self._update_fields:
            flag_sql = self._db.dialect.get_upsert_inserted_flag_sql()
            if flag_sql is not None:
                terms.append(LiteralValue(flag_sql, alias=UPSERT_INSERTED_FLAG_ALIAS))
        return terms

    async def _read_existing_primary_keys(self) -> None:
        """Reads which objects' rows exist before an upsert writes - on a database without an
        inserted-row flag, so the report can tell an updated row from an inserted one."""
        # Deferred import: the queryset package builds bulk_create() queries.
        from hare.query.queryset.queryset import QuerySet

        meta = self.model._meta
        conflict_names = self._get_conflict_update_key_field_names()
        keys = {tuple(getattr(obj, name) for name in conflict_names) for obj in self._objects}
        existing = await (
            QuerySet(self.model)
            .using(self._db)
            .filter(**{f"{conflict_names[0]}__in": [key[0] for key in keys]})
            .values_list(*meta.pk_attr_names, *conflict_names)
        )
        pk_count = len(meta.pk_attr_names)
        self._existing_primary_keys = {tuple(row[:pk_count]) for row in existing if tuple(row[pk_count:]) in keys}

    async def _report_bulk_create(self, create: Awaitable[None]) -> None:
        """Runs the insert and reports its rows (``ChangeEvents``) - of a failed insert, only the
        batches a ``COPY`` outside the transaction committed. Where conflicting rows are skipped or
        updated, the returned rows tell inserted from updated.

        Args:
            create: The insert.
        """
        listened = ChangeEvents.is_observed(self.model)
        if (
            listened
            and (self._ignore_conflicts or self._update_fields)
            and self._db.features.supports_returning
            and not self._use_copy
            and self.model._meta.has_primary_key
        ):
            self._reported_rows = []
            if self._update_fields and self._db.dialect.get_upsert_inserted_flag_sql() is None:
                await self._read_existing_primary_keys()
        try:
            await create
        except BaseException:
            committed_objects = getattr(self, "_rows_committed_outside_transaction", None)
            if committed_objects:
                await self._report_inserted_objects(list(committed_objects))
            raise
        if self._reported_rows is None:
            await self._report_inserted_objects(list(self._objects))
            if self._update_fields and listened and self._objects:
                await WriteSteps.report(self._db, self.model, RowOperation.UPDATE, fields=self._update_fields)
            return
        await self._report_returned_rows()

    async def _report_returned_rows(self) -> None:
        """Reports the rows the statements returned - inserted, or updated by an upsert."""
        meta = self.model._meta
        pk_columns = [meta.fields_db_projection[name] for name in meta.pk_attr_names]
        pk_fields = [meta.fields_map[name] for name in meta.pk_attr_names]
        types = self._db.dialect.types
        inserted_pks: list[Any] = []
        updated_pks: list[Any] = []
        existing_primary_keys = self._existing_primary_keys
        for row in self._reported_rows or ():
            key = tuple(
                types.get_python_value(field, row[column]) for field, column in zip(pk_fields, pk_columns, strict=True)
            )
            pk = key if len(key) > 1 else key[0]
            if UPSERT_INSERTED_FLAG_ALIAS in row:
                inserted = bool(row[UPSERT_INSERTED_FLAG_ALIAS])
            else:
                inserted = existing_primary_keys is None or key not in existing_primary_keys
            (inserted_pks if inserted else updated_pks).append(pk)
        if inserted_pks:
            await WriteSteps.report(self._db, self.model, RowOperation.INSERT, pks=inserted_pks)
        if updated_pks:
            await WriteSteps.report(
                self._db, self.model, RowOperation.UPDATE, pks=updated_pks, fields=self._update_fields
            )

    async def _report_inserted_objects(self, objects: list[TModel]) -> None:
        """Reports the rows of inserted objects (``ChangeEvents``).

        Args:
            objects: The objects.
        """
        await WriteSteps.report(self._db, self.model, RowOperation.INSERT, instances=objects)

    def _scope_to_active_tenant(self) -> None:
        """Gives each object without a tenant the scope's one value and limits an ON CONFLICT DO
        UPDATE to the rows of the scope's tenants.

        Raises:
            QueryError: The model has no tenant scope, an object carries a tenant outside it, or
                an object carries none under a scope of several values.
        """
        if not self._tenant_scoped:
            return
        scope = WriteSteps.scope_to_active_tenant(
            self.model, self._objects, "bulk_create", fills_missing=True, requires_active=True
        )
        if self._update_fields:
            self._conflict_update_tenants = Tenancy.get_values(scope)

    async def _run(self) -> None:
        self._writer = InstanceWriter(self.model, self._db)
        self._objects = list(self._objects)  # materialize for multi-pass
        self._scope_to_active_tenant()

        # A pending async default is resolved before the values are read, as save() does.
        for obj in self._objects:
            if obj._await_when_save:
                await obj._set_async_default_field()
        await Tenancy.check_objects_relation_targets(self.model, self._objects, self._db)

        # Captured before the objects are serialized: to_db_value() stamps auto_now fields on the
        # instance.
        stamped_values = InstanceValues()
        stamped_values.capture(self._objects, WriteFields.of(self.model).stamped_on_insert_names)

        track_dirty_fields = self.model._meta.track_dirty_fields
        # Each object's state before the write - (_saved_in_db, pk, dirty snapshot,
        # _custom_generated_pk, _db_connection_name) - put back when the call fails or its
        # transaction rolls back.
        pk_attr = self.model._meta.pk_attr
        pre_write_states: list[tuple[bool, Any, dict[str, Any] | None, bool, str | None]]
        if not track_dirty_fields and type(pk_attr) is str:
            # Read by attrgetter, without a Python call per object.
            objects = self._objects
            pre_write_states = list(
                zip(
                    map(attrgetter("_saved_in_db"), objects),
                    map(attrgetter(pk_attr), objects),
                    repeat(None),
                    map(attrgetter("_custom_generated_pk"), objects),
                    map(attrgetter("_db_connection_name"), objects),
                )
            )
        else:
            pre_write_states = [
                (
                    obj._saved_in_db,
                    obj.pk,
                    dict(obj._dirty_snapshot) if track_dirty_fields and obj._dirty_snapshot is not None else None,
                    obj._custom_generated_pk,
                    obj._db_connection_name,
                )
                for obj in self._objects
            ]

        omit_fields = self._analyze_db_default_fields(self._writer.regular_columns)
        # Fields a RETURNING row sets on an object while later statements of this call can still fail.
        returned_field_names = self._get_returned_field_names(omit_fields) if self._returning else []
        returned_values = InstanceValues()
        returned_values.capture(self._objects, returned_field_names)

        insert_sql, insert_sql_all = self._make_queries(omit_fields)
        effective_columns = [c for c in self._writer.regular_columns if c not in omit_fields]
        effective_columns_all = [c for c in self._writer.regular_columns_all if c not in omit_fields]
        if self._use_copy:
            self._check_copy_supported_types(omit_fields)
            self._check_copy_groups_supported()
        self._rows_committed_outside_transaction = []
        try:
            if (not self._use_copy or self._db.copy_joins_transaction) and self._runs_multiple_statements(
                effective_columns, effective_columns_all, omit_fields
            ):
                # Several statements run in one transaction - a failure leaves no row of the earlier
                # ones. A COPY that can't join a transaction commits each batch on its own.
                original_db = self._db
                async with original_db._in_transaction() as transaction_db:
                    self._apply_db(transaction_db)
                    try:
                        await self._execute_writes(
                            insert_sql, insert_sql_all, effective_columns, effective_columns_all, omit_fields
                        )
                    finally:
                        self._apply_db(original_db)
            else:
                await self._execute_writes(
                    insert_sql, insert_sql_all, effective_columns, effective_columns_all, omit_fields
                )
        except BaseException:
            # The rows of earlier statements were rolled back with the failed one - the objects
            # they populated go back to their pre-call state too. Rows a COPY committed on its own
            # connection stay written, so their objects are marked saved instead.
            committed_object_ids = {id(obj) for obj in self._rows_committed_outside_transaction}
            for obj, pre_write_state in zip(self._objects, pre_write_states, strict=True):
                if id(obj) in committed_object_ids:
                    object.__setattr__(obj, "_saved_in_db", True)
                    obj._remember_db(self._db)
                    continue
                was_saved_in_db, _, _, custom_generated_pk, db_connection_name = pre_write_state
                returned_values.restore((obj,))
                stamped_values.restore((obj,))
                object.__setattr__(obj, "_saved_in_db", was_saved_in_db)
                object.__setattr__(obj, "_custom_generated_pk", custom_generated_pk)
                object.__setattr__(obj, "_db_connection_name", db_connection_name)
            raise

        # Every inserted object is marked saved - unless ON CONFLICT may have skipped its row: then
        # only the objects a RETURNING row was matched to are. Restores for a rollback are
        # registered only inside a transaction.
        restores_on_rollback = isinstance(self._db, TransactionClient)
        # What Model._remember_db() records, set directly on every object.
        connection_name = self._db.connection_name
        if self._use_copy or not self._may_skip_conflicting_rows:
            for obj in self._objects:
                # A later rollback reverts the rows, not the instances - _saved_in_db and the
                # stamped values are registered to be put back.
                if restores_on_rollback:
                    obj._register_rollback_restore(self._db, "_saved_in_db", False)
                    stamped_values.keep(self._db, (obj,))
                object.__setattr__(obj, "_saved_in_db", True)
                object.__setattr__(obj, "_db_connection_name", connection_name)

        # The inserted values are the objects' dirty-tracking baseline - unless ON CONFLICT may have
        # skipped a row: which objects were written isn't known then, and they stay dirty.
        if track_dirty_fields and not self._may_skip_conflicting_rows:
            for obj in self._objects:
                obj._snapshot_dirty_fields()

        if not restores_on_rollback:
            return
        for obj, pre_write_state in zip(self._objects, pre_write_states, strict=True):
            if not obj._saved_in_db:
                continue
            was_saved_in_db, old_pk, old_dirty_snapshot, _, _ = pre_write_state
            obj._register_rollback_restore(self._db, "_saved_in_db", was_saved_in_db)
            if obj.pk != old_pk:
                obj._register_rollback_restore(self._db, "pk", old_pk)
            if track_dirty_fields:
                obj._register_rollback_restore(self._db, "_dirty_snapshot", old_dirty_snapshot)

    def _get_statements(self, params_inline: bool) -> list[tuple[str, list[Any]]]:
        """The INSERT statements the query runs - bound, each holds the row of the first object of
        its group (objects without and with a caller-given primary key).

        Args:
            params_inline: Whether values are rendered into the SQL instead of bound.

        Returns:
            ``(sql, bound values)`` of each statement.
        """
        self._validate_dialect_dependent_options()
        self._writer = InstanceWriter(self.model, self._db)
        self._objects = list(self._objects)
        self._scope_to_active_tenant()
        omit_fields = self._analyze_db_default_fields(self._writer.regular_columns)
        if params_inline:
            return [(sql, []) for sql in self._make_inline_statements(omit_fields)]
        statements: list[tuple[str, list[Any]]] = []
        for (sql, statement_values), custom_generated_pk in zip(
            self._make_template_statements(omit_fields), (False, True), strict=True
        ):
            group = [obj for obj in self._objects if obj._custom_generated_pk == custom_generated_pk]
            if group:
                field_names = self._filtered_field_names(omit_fields, include_generated=custom_generated_pk)
                rows = self._serialize_instances(group[:1], field_names)
                statements.append((sql, [*(rows[0] if rows else []), *statement_values]))
        return statements
