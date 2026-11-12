from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from copy import copy
from itertools import chain, compress
from operator import not_
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from hare.core.registries import Registries
from hare.core.routing.written_connections import WrittenConnections
from hare.exceptions import QueryError, StaleObjectError, UnSupportedError, ValidationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.instrumentation.capture.captured_write import CapturedWrite
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.instances.instance_saving import InstanceSaving
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.instance_capture import InstanceCapture
from hare.models.write.instance_values import InstanceValues
from hare.models.write.returned_values import ReturnedValues
from hare.models.write.rules.constants import BULK_UPDATE_METHOD_NAME
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.models.write.rules.tenant_write_rules import TenantWriteRules
from hare.models.write.rules.written_target_checks import WrittenTargetChecks
from hare.models.write.write_finalizer import WriteFinalizer
from hare.models.write.write_steps import WriteSteps
from hare.query.execution.bound_write import BoundWrite
from hare.query.execution.write_statements import WritePreview, WriteStatement
from hare.query.expressions import Q
from hare.query.hare_column_names import HareColumnNames
from hare.query.key_columns import KeyColumns
from hare.query.queryset.arguments.collection_arguments import CollectionArguments
from hare.query.scopes.tenants.tenant_scope import TenantScope
from hare.query.statements.building.query_conditions import QueryConditions
from hare.query.statements.building.query_ctes import QueryCtes
from hare.query.statements.building.query_grouping import QueryGrouping
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.constants import (
    BULK_UPDATE_DEFERRED_PREVIEW_MESSAGE,
    BULK_UPDATE_MATCHES_FILTER_ALIAS,
    BULK_UPDATE_RETURNING_UNSUPPORTED_MESSAGE,
    BULK_UPDATE_VALUES_ALIAS,
)
from hare.query.statements.write.bulk.bulk_objects import BulkObjects
from hare.query.statements.write.bulk.bulk_update_checks import BulkUpdateChecks
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.bulk.declarations import BoundUpdateBatch, BulkUpdateRun, BulkUpdateStatementLayout
from hare.query.statements.write.update_query import UpdateQuery
from hare.sql.builder.queries.query_sql_rendering import QuerySqlRendering
from hare.sql.builder.returned_value import ReturnedValue
from hare.sql.builder.update_from_values import UpdateFromValues, ValuesColumn
from hare.sql.functions.cast import Cast
from hare.sql.identifiers import Identifiers
from hare.sql.terms.case.case import Case
from hare.sql.terms.parameters.parameterizer import Parameterizer
from hare.sql.terms.parameters.query_parameters import QueryParameters
from hare.sql.terms.term import Term
from hare.sql.terms.values.literal_value import LiteralValue

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.sql.sql_context import SqlContext

TModel = TypeVar("TModel", bound="Model")


class BulkUpdateQuery(UpdateQuery, Generic[TModel]):
    """``queryset.bulk_update()`` - the given fields of each object written to its row, by batches."""

    __slots__ = (
        "fields",
        "_objects",
        "_batch_size",
        "_returning",
        "_returning_explicitly_requested",
        "_tenant_scoped",
    )

    def __init__(
        self,
        source: QuerySet[Any, Any],
        objects: Iterable[TModel],
        fields: Iterable[str],
        batch_size: int | None = None,
        returning: bool | None = None,
    ):
        """
        Args:
            source: The queryset whose rows the objects are written among.
            objects: The instances to write.
            fields: The fields to write.
            batch_size: How many instances one statement writes.
            returning: Read the written rows back into the instances - ``Meta.returning`` when None.

        Raises:
            QueryError: An object or a field can't be written by ``bulk_update()``.
            IncompleteInstanceError: A partially loaded object lacks a field the update reads.
            FieldError: A name in ``fields`` isn't a field of the model.
        """
        BulkWriteBatches.check_batch_size(batch_size, "bulk_update")
        WrittenTargetChecks.raise_if_refused(source, "bulk_update")
        model = source.model
        returning_explicitly_requested = returning is not None
        returning = model._meta.returning if returning is None else returning
        objects = CollectionArguments.get_list(objects, "bulk_update()")
        field_names = CollectionArguments.get_list(fields, f"bulk_update() on {model.__name__}: fields")
        if not field_names:
            raise QueryError(f"bulk_update() on {model.__name__} needs at least one field to update")
        rules = RowWriteRules.get(model, WriteType.UPDATE, field_names, BULK_UPDATE_METHOD_NAME)
        named_field_names: tuple[str, ...] = rules.named_field_names  # type: ignore[assignment]
        BulkObjects.validate(
            model, objects, "bulk_update", [*named_field_names, *model._meta.primary_key_attribute_names]
        )
        tenant_scoped = bool(model._meta.tenant_field) and not source._visibility.all_tenants
        if tenant_scoped:
            BulkUpdateChecks.check_tenant_defaults_ready(model, objects)
        BulkUpdateChecks.check_primary_keys(model, objects)
        if source._uses_default_scope:
            # The objects are matched by their keys, like save() matches one: under the default
            # scope a soft-deleted object would match no row. Their tenant is checked above.
            source = source._clone()
            source._uses_default_scope = False
        super().__init__(source, {})
        #: What the update writes - the fields named, the blind indexes and the stamps.
        self._rules = rules
        self.fields = rules.get_written_names()
        self._objects = objects
        self._batch_size = batch_size
        self._returning = returning
        self._returning_explicitly_requested = returning_explicitly_requested
        # Whether the rows belong to the tenant active when the query runs (Meta.tenant_field set,
        # no .all_tenants()).
        self._tenant_scoped = tenant_scoped

    def _make_queries(
        self, run: BulkUpdateRun[TModel], parameters_inline: bool = False
    ) -> list[BoundUpdateBatch[TModel]]:
        """One UPDATE over a VALUES table of the rows per batch of objects - a single pass, instead of
        a CASE per field. One multi-row UPDATE per batch is one round trip.

        Args:
            parameters_inline: Write every value into the SQL text instead of binding it.

        Returns:
            Each statement, its parameters and the objects it writes.

        Raises:
            UnSupportedError: A field is a key column the database doesn't update in place.
        """
        dialect = self.dialect
        if self._returning and self._returning_explicitly_requested and not dialect.features.supports_returning:
            raise UnSupportedError(BULK_UPDATE_RETURNING_UNSUPPORTED_MESSAGE.format(dialect=dialect))
        table_options = self.model._meta.get_table_options(dialect)
        if table_options is not None:
            table_options.raise_if_fixed_fields_written(self.model, self.fields)
        BulkUpdateChecks.raise_if_row_written_twice(self.model, run.objects, dialect.types)
        layout = self._get_statement_layout(run, parameters_inline=parameters_inline)
        queries_with_parameters: list[BoundUpdateBatch[TModel]] = []
        for batch in BulkWriteBatches.get_batches(run.objects, layout.batch_size):
            objects_item = list(batch)
            if not objects_item:
                continue
            if not parameters_inline:
                queries_with_parameters.append(self._make_whole_rows_query(layout, objects_item))
            else:
                queries_with_parameters.append(self._make_inline_query(layout, objects_item))
        return queries_with_parameters

    def _get_filter_criterion(self, tenant_condition: Q | None = None) -> Term | None:
        """The queryset's own filter as a criterion over the base table alone - through a primary
        key subquery where it needs a JOIN, which the hand-built ``UPDATE ... FROM (VALUES ...)``
        can't carry. The query's CTEs are applied after it, so the WITH clause is rendered once,
        ahead of the UPDATE, rather than inside that subquery too.

        Returns:
            The criterion, None when the queryset carries no filter.
        """
        # Reset first: a rebuild would take every join as already made.
        QueryJoins.reset_joined_tables(self)
        # Built as a SELECT first: QueryConditions.get_filters() needs somewhere to attach a JOIN.
        self._apply_ambient_scope()
        if tenant_condition is not None:
            self._ambient_conditions = (*self._ambient_conditions, tenant_condition)
        self.query = self._get_base_query()
        QueryConditions.get_filters(self)
        # A filter on an aggregate annotation needs its GROUP BY to be applied per row.
        QueryGrouping.apply_auto_group_by(self)
        if self._filters_need_primary_key_subquery():
            # The annotation terms QueryConditions.get_filters() selected would become extra subquery columns.
            self.query._selects = []
            filter_criterion: Term | None = self._get_matching_rows_criterion(self.query)
        else:
            filter_criterion = self.query._wheres
        QueryCtes.apply_with_ctes(self)
        return filter_criterion or None

    def _get_returning_columns(self, pk_columns: list[str]) -> list[str]:
        """The columns the UPDATE returns: the primary key - the rows come in no order - and, with
        returning=True, the generated columns. The key alone is returned for a model with an
        optimistic lock field, dirty tracking or an updated ``auto_now`` field: only the objects whose
        rows were written get their version, baseline and stamps.

        Args:
            pk_columns: The primary key's columns.

        Returns:
            The columns, none when nothing is read back.
        """
        meta = self.model._meta
        if not self._connection.dialect.features.supports_returning:
            return []
        if self._returning and meta.generated_db_fields:
            return list(dict.fromkeys(pk_columns + list(meta.generated_db_fields)))
        if self._reads_written_keys():
            return list(pk_columns)
        return []

    def _reads_written_keys(self) -> bool:
        """Whether the UPDATE returns the keys of the rows it wrote, and nothing else - for a model with
        an optimistic lock field, dirty tracking or an updated ``auto_now`` field.

        Returns:
            Whether it does.
        """
        meta = self.model._meta
        # An auto_now field stamped on an instance has to be put back when its row wasn't written.
        has_auto_now_field = any(getattr(meta.fields_map[field_name], "auto_now", False) for field_name in self.fields)
        return bool(meta.optimistic_lock_field or meta.track_dirty_fields or has_auto_now_field)

    def _get_written_column_fields(self) -> list[str]:
        """The column fields the UPDATE writes - a relation through its key column field(s), every
        one of them for a composite key; a relation and its own key field named together write the
        column once.

        Returns:
            The field names.
        """
        fields: list[str] = []
        for field in self.fields:
            field_object = self.model._meta.fields_map[field]
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                fields.extend(field_object.source_fields)
            else:
                fields.append(field)
        return list(dict.fromkeys(fields))

    def _get_statement_layout(
        self, run: BulkUpdateRun[TModel], *, parameters_inline: bool
    ) -> BulkUpdateStatementLayout:
        """What every statement of this call shares.

        Args:
            parameters_inline: Whether every value is written into the SQL text.

        Returns:
            The layout.
        """
        meta = self.model._meta
        table = meta.basetable
        filter_criterion = run.filter_criterion = self._get_filter_criterion(run.tenant_condition)
        # One PK column for a regular model, N (in primary_key_attribute order) for a composite one -
        # everything below is written to that general case, a single-column PK is just N=1 of it.
        primary_key_attribute_names = meta.primary_key_attribute_names
        pk_field_objects = [meta.fields_map[name] for name in primary_key_attribute_names]
        pk_columns = list(KeyColumns.get_source_columns(meta))
        fields = self._get_written_column_fields()
        field_objects = [meta.fields_map[field] for field in fields]
        dialect = self._connection.dialect
        features = self._connection.features
        get_cast_type = dialect.parameters.get_field_parameter_cast_type
        # A database may not infer a column's type from a VALUES row that mixes types across
        # columns - the dialect names the cast every column needs then, the pk column(s) included.
        field_cast_types = [get_cast_type(field_object) for field_object in field_objects]
        # Optimistic locking: the old version is per object, so it rides in the VALUES row after the
        # key and is matched in WHERE like the key; the bump is the same SET for every row.
        optimistic_lock_field = meta.optimistic_lock_field
        optimistic_lock_field_object = meta.fields_map[optimistic_lock_field] if optimistic_lock_field else None
        get_key_cast_type = dialect.parameters.get_row_key_cast_type
        key_cast_types = [get_key_cast_type(field_object) for field_object in pk_field_objects]
        key_fields = list(primary_key_attribute_names)
        version_column = None
        if optimistic_lock_field_object is not None and optimistic_lock_field:
            version_column = optimistic_lock_field_object.source_field or optimistic_lock_field
            key_cast_types.append(get_key_cast_type(optimistic_lock_field_object))
            key_fields.append(optimistic_lock_field)

        base_context = self._connection.sql_context
        quote: Callable[[str], str] = base_context.quote
        db_table = meta.db_table
        values_alias = Identifiers((db_table,)).get_free_name(BULK_UPDATE_VALUES_ALIAS)
        # c0..c{key column count - 1} = the key column(s) and the old version, the rest the written
        # fields.
        value_column_names = [f"c{column_position}" for column_position in range(len(key_fields) + len(fields))]
        clauses = self._connection.dialect.clauses
        # Table-qualified on the right-hand side and in WHERE, so a model column named like one of
        # the VALUES table's own c<N> columns can't be read from the wrong side.
        table_sql = quote(db_table)
        assignments: list[tuple[str, ValuesColumn | Term]] = [
            (field_object.source_field or field, ValuesColumn(index))
            for index, (field_object, field) in enumerate(
                zip(field_objects, fields, strict=True), start=len(key_fields)
            )
        ]
        matches = tuple(
            (column, ValuesColumn(index))
            for index, column in enumerate([*pk_columns, *([version_column] if version_column is not None else [])])
        )
        if version_column is not None:
            assignments.append((version_column, table[version_column] + LiteralValue("1")))
        # A value's placeholder depends only on the dialect and the cast type - one template per
        # column, formatted with the parameter index, instead of a Parameter and Cast term per value.
        placeholder_template = dialect.parameters.placeholder_template
        key_placeholder_templates = [
            clauses.get_typed_placeholder_template(placeholder_template, cast_type) for cast_type in key_cast_types
        ]
        field_placeholder_templates = [
            clauses.get_typed_placeholder_template(placeholder_template, cast_type) for cast_type in field_cast_types
        ]
        # Inlined SQL binds nothing, so only the parameterized path is limited by the bind-parameter
        # ceiling - less what every statement binds besides its rows.
        batch_size = (
            self._batch_size
            if parameters_inline
            else BulkWriteBatches.get_bind_parameter_safe_batch_size(
                self._batch_size,
                len(value_column_names),
                features.max_bind_parameters - self._get_fixed_parameter_count(base_context, filter_criterion),
            )
        )
        if (max_rows := features.max_bulk_update_rows) is not None:
            batch_size = max_rows if batch_size is None else min(batch_size, max_rows)
        return BulkUpdateStatementLayout(
            write=UpdateFromValues(
                table=table,
                assignments=tuple(assignments),
                matches=matches,
                columns=tuple(value_column_names),
                alias=values_alias,
                condition=filter_criterion,
                returned=tuple(
                    ReturnedValue(sql=f"{table_sql}.{quote(column)}", alias_sql=quote(column))
                    for column in self._get_returning_columns(pk_columns)
                ),
            ),
            base_context=base_context,
            row_cast_types=[*key_cast_types, *field_cast_types],
            row_fields=[*key_fields, *fields],
            row_placeholder_templates=tuple(key_placeholder_templates + field_placeholder_templates),
            batch_size=batch_size,
        )

    def _get_update_sql(
        self, layout: BulkUpdateStatementLayout, values_context: SqlContext, with_sql: str, values_sql: str
    ) -> str:
        """The UPDATE of one batch.

        Args:
            layout: What every statement of this call shares.
            values_context: The context the batch's values render in.
            with_sql: The WITH clause.
            values_sql: The VALUES rows.

        Returns:
            The statement.
        """
        return self._connection.dialect.clauses.get_update_from_values_sql(
            write=layout.write, with_sql=with_sql, values_sql=values_sql, sql_context=values_context
        )

    def _get_hidden_values(
        self, layout: BulkUpdateStatementLayout, values: list[Any], first_row_value: int, row_count: int
    ) -> list[Any]:
        """A statement's parameters with the ``sensitive=True`` fields' values never shown.

        Args:
            layout: What every statement of this call shares.
            values: The parameters.
            first_row_value: Where the rows' values start among them.
            row_count: The rows.

        Returns:
            The parameters - a ``QueryParameters`` when a sensitive field is written.
        """
        if not self.model._meta.sensitive_fields:
            return values
        sensitive_positions = BulkWriteBatches.get_sensitive_positions(self.model, layout.row_fields)
        if not sensitive_positions:
            return values
        row_width = len(layout.row_fields)
        return QueryParameters(
            values,
            [
                first_row_value + row_index * row_width + position
                for row_index in range(row_count)
                for position in sensitive_positions
            ],
        )

    def _make_whole_rows_query(
        self, layout: BulkUpdateStatementLayout, objects_item: list[TModel]
    ) -> BoundUpdateBatch[TModel]:
        """The UPDATE of one batch whose rows the field codecs serialize whole - the key, the old
        version and the fields of every row in one serialization; the VALUES text depends only on
        the placeholders and the row count.

        Args:
            layout: What every statement of this call shares.
            objects_item: The batch's objects.

        Returns:
            A bound statement and the objects to which its result applies.
        """
        parameterizer = Parameterizer()
        values_context = layout.base_context.copy(parameterizer=parameterizer)
        # The WITH clause is rendered first - its parameters come first.
        with_sql = QuerySqlRendering.with_sql(self.query._with, values_context) if self.query._with else ""
        row_fields = layout.row_fields
        values_before_rows = len(parameterizer.values)
        rows = BulkWriteBatches.serialize_instances(
            self.model, self._connection.dialect.types, objects_item, row_fields
        )
        row_count = len(rows)
        values_sql = BulkWriteBatches.get_values_rows_sql(
            self.model, layout.row_placeholder_templates, values_before_rows + 1, row_count
        )
        parameterizer.values.extend(chain.from_iterable(rows))
        update_sql = self._get_update_sql(layout, values_context, with_sql, values_sql)
        statement_values = self._get_hidden_values(layout, parameterizer.values, values_before_rows, row_count)
        return BoundUpdateBatch(
            WriteStatement(
                update_sql,
                statement_values,
                bool(layout.write.returned),
                recovery=self._connection.get_write_recovery(self.model._meta.basetable),
            ),
            objects_item,
        )

    def _make_inline_query(
        self, layout: BulkUpdateStatementLayout, objects_item: list[TModel]
    ) -> BoundUpdateBatch[TModel]:
        """The UPDATE of one batch with every value rendered as a literal.

        Args:
            layout: What every statement of this call shares.
            objects_item: The batch's objects.

        Returns:
            A bound statement and the objects to which its result applies.
        """
        values_context = layout.base_context
        with_sql = QuerySqlRendering.with_sql(self.query._with, values_context) if self.query._with else ""
        rows = BulkWriteBatches.serialize_instances(
            self.model, self._connection.dialect.types, objects_item, layout.row_fields
        )
        rows_sql = []
        for values in rows:
            row_parts = []
            for value, cast_type in zip(values, layout.row_cast_types, strict=True):
                wrapped = self.query._wrapper_class(value)
                term = Cast(wrapped, cast_type) if cast_type is not None else wrapped
                row_parts.append(term.get_sql(values_context))
            rows_sql.append("(" + ", ".join(row_parts) + ")")
        update_sql = self._get_update_sql(layout, values_context, with_sql, ", ".join(rows_sql))
        return BoundUpdateBatch(
            WriteStatement(
                update_sql,
                [],
                bool(layout.write.returned),
                recovery=self._connection.get_write_recovery(self.model._meta.basetable),
            ),
            objects_item,
        )

    def _get_fixed_parameter_count(self, base_context: Any, extra_where: Term | None) -> int:
        """Bind parameters every statement of this call carries besides its VALUES rows.

        Args:
            base_context: The dialect's base SQL context.
            extra_where: The queryset's own filter, or None.

        Returns:
            The parameter count of the CTE bodies and the filter.
        """
        parameterizer = Parameterizer()
        fixed_context = base_context.copy(parameterizer=parameterizer)
        if self.query._with:
            QuerySqlRendering.with_sql(self.query._with, fixed_context)
        if extra_where:
            extra_where.get_sql(fixed_context.copy(subquery=True, with_namespace=True))
        return len(parameterizer.values)

    async def _count_stale_objects(
        self, not_updated_objects: list[TModel], run: BulkUpdateRun[TModel], connection: DatabaseClient
    ) -> int:
        """Counts the objects whose row the UPDATE skipped for a stale ``Meta.optimistic_lock_field``,
        rather than for the queryset's own filter.

        Args:
            not_updated_objects: Objects of one batch whose row the UPDATE didn't touch.

        Returns:
            How many of them are stale - a row that no longer exists counts too.
        """
        if not not_updated_objects or run.filter_criterion is None:
            return len(not_updated_objects)
        primary_key_attribute_names = self.model._meta.primary_key_attribute_names
        pk_field_objs = [self.model._meta.fields_map[name] for name in primary_key_attribute_names]
        pk_columns = KeyColumns.get_source_columns(self.model._meta)
        table = self.model._meta.basetable
        pk_terms = [table[column] for column in pk_columns]
        pk_values = [obj.pk if len(primary_key_attribute_names) > 1 else (obj.pk,) for obj in not_updated_objects]
        db_pk_values = KeyColumns.write_rows(pk_field_objs, pk_values, self.dialect.types, None)
        pk_condition = KeyColumns.row_is_in(pk_terms, db_pk_values)
        matches_filter_column = HareColumnNames.get_free_name(BULK_UPDATE_MATCHES_FILTER_ALIAS, (self.model,))
        matches_filter = (
            Case(alias=matches_filter_column).when(run.filter_criterion, LiteralValue("1")).else_(LiteralValue("0"))
        )
        query = connection.query_class.from_(table).select(*pk_terms, matches_filter).where(pk_condition)
        query._with = list(self.query._with)
        _, rows = await connection.execute(*query.get_parameterized_sql())
        excluded_by_filter_count = sum(1 for row in rows if not int(dict(row)[matches_filter_column]))
        return len(not_updated_objects) - excluded_by_filter_count

    async def _execute_many(
        self,
        run: BulkUpdateRun[TModel],
        queries_with_parameters: list[BoundUpdateBatch[TModel]],
        stamped_values: InstanceValues | None = None,
    ) -> int | None:
        """Runs every statement, then raises for the stale objects found across all of them.

        Args:
            queries_with_parameters: The bound statements and their input objects.
            stamped_values: The auto_now values serialization stamped on the objects.

        Returns:
            The number of updated rows.

        Raises:
            StaleObjectError: An object had a stale ``Meta.optimistic_lock_field``.
        """
        count, stale_object_count = await self._execute_statements(
            run, run.connection, queries_with_parameters, stamped_values
        )
        self._raise_for_stale_objects(stale_object_count)
        return count

    def _raise_for_stale_objects(self, stale_object_count: int) -> None:
        """Raises when any object of this call had a stale ``Meta.optimistic_lock_field``.

        Args:
            stale_object_count: How many objects across every statement were stale.

        Raises:
            StaleObjectError: ``stale_object_count`` is not zero.
        """
        if stale_object_count:
            # No single pk/expected_version to attach here - unlike save()/delete()/restore()
            # (always exactly one instance), a batch UPDATE's affected-rowcount alone can't tell
            # WHICH object(s) across the whole call were stale, only how many.
            raise StaleObjectError(
                f"bulk_update() on {self.model.__name__}: {stale_object_count} object(s) "
                "across this call had a stale version",
                self.model,
                None,
                None,
            )

    async def _execute_statements(
        self,
        run: BulkUpdateRun[TModel],
        connection: DatabaseClient,
        queries_with_parameters: list[BoundUpdateBatch[TModel]],
        stamped_values: InstanceValues | None = None,
    ) -> tuple[int | None, int]:
        """Runs every statement of this call, syncing each updated object's in-memory state - every
        object counts as written where the database returns no row.

        Args:
            queries_with_parameters: The bound statements and their input objects.
            stamped_values: The auto_now values serialization stamped on the objects.

        Returns:
            The number of updated rows - None where the database reports none - and the number of
            stale objects across every statement.
        """
        optimistic_lock_field = self.model._meta.optimistic_lock_field
        track_dirty = self.model._meta.track_dirty_fields
        fields_to_sync = self._rules.get_updated_names() if track_dirty else ()
        primary_key_attribute_names = self.model._meta.primary_key_attribute_names
        source_primary_key_attributes = KeyColumns.get_source_columns(self.model._meta)
        populate_generated_fields = bool(self._returning and self.model._meta.generated_db_fields)
        supports_returning = self.dialect.features.supports_returning
        stamped_values = stamped_values or InstanceValues()
        count: int | None = 0
        # Counted over every chunk - raised once all were attempted.
        total_stale_object_count = 0
        for batch in queries_with_parameters:
            objects_item = batch.objects
            stamped_values.keep(connection, objects_item)
            rows_affected, returned_rows = await BoundWrite.execute_command(connection, batch.command)
            run.completed_objects.extend(objects_item)
            stale_object_count = 0
            written_objects = objects_item
            if optimistic_lock_field or track_dirty or stamped_values.old_values:
                # RETURNING names the rows the UPDATE wrote. Only those objects get their version
                # and dirty baseline updated; any other gets its stamped auto_now values put back.
                # Without RETURNING every object counts as written.
                if supports_returning:
                    written_ids = {
                        id(obj)
                        for obj, _row in ReturnedValues.match_rows(
                            self.model, self.dialect.types, objects_item, returned_rows, primary_key_attribute_names
                        )
                    }
                    is_written = [id(obj) in written_ids for obj in objects_item]
                else:
                    is_written = [True] * len(objects_item)
                # Only the written fields become clean; other unsaved changes remain dirty.
                written_objects = list(compress(objects_item, is_written))
                unwritten_objects = list(compress(objects_item, map(not_, is_written)))
                run.unwritten_objects.extend(unwritten_objects)
                if optimistic_lock_field or track_dirty:
                    for obj in written_objects:
                        # The version bump is registered to be put back if the transaction rolls back.
                        if optimistic_lock_field:
                            stamped_values.bump_optimistic_lock(obj, optimistic_lock_field, connection)
                        if track_dirty:
                            DirtyFields.sync_dirty_snapshot_fields(obj, fields_to_sync, connection)
                if stamped_values.old_values:
                    stamped_values.keep(connection, written_objects)
                    if unwritten_objects:
                        stamped_values.restore(unwritten_objects)
                if optimistic_lock_field and rows_affected is not None and rows_affected < len(objects_item):
                    stale_object_count = await self._count_stale_objects(unwritten_objects, run, connection)
            if self.model._meta.recomputed_db_fields and not (populate_generated_fields and supports_returning):
                ReturnedValues.leave_unloaded(
                    self.model, written_objects, self.model._meta.recomputed_db_fields, connection=connection
                )
            elif populate_generated_fields:
                # UPDATE ... FROM (VALUES ...) returns its rows in no particular order - matched by
                # primary key; the key itself isn't set again.
                readers = None
                for obj, row in ReturnedValues.match_rows(
                    self.model, self.dialect.types, objects_item, returned_rows, primary_key_attribute_names
                ):
                    if readers is None:
                        readers = ReturnedValues.get_returning_readers(
                            self.model,
                            self.dialect.types,
                            (column for column in row if column not in source_primary_key_attributes),
                        )
                    ReturnedValues.apply_row(
                        self.model,
                        self.dialect.types,
                        obj,
                        row,
                        connection=connection,
                        readers=readers,
                    )
            if stale_object_count:
                # Every chunk is its own UPDATE - a stale object in one doesn't stop the others;
                # raised after the loop.
                total_stale_object_count += stale_object_count
            count = None if count is None or rows_affected is None else count + rows_affected
        return count, total_stale_object_count

    async def _execute_bound(self) -> int | None:
        if self._is_none:
            return await self._execute_none()
        connection = self.get_connection(for_write=True)
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection.connection_alias)
        if (
            self._returning
            and self._returning_explicitly_requested
            and not connection.dialect.features.supports_returning
        ):
            raise UnSupportedError(BULK_UPDATE_RETURNING_UNSUPPORTED_MESSAGE.format(dialect=connection.dialect))
        capture_needs = self.model._meta.change_capture_needs
        if capture_needs is not None and capture_needs.captures(RowOperation.UPDATE):
            return await WriteSteps.get_whole_run(
                connection, self._run_captured_bulk_update(capture_needs, connection)
            )
        run = BulkUpdateRun(connection, list(self._objects))
        return await WriteSteps.get_whole_run(connection, self._report_bulk_update(run, self._run(run)))

    async def _run_captured_bulk_update(self, capture_needs: CaptureNeeds, connection: DatabaseClient) -> int | None:
        """Runs the update of a model with ``Meta.change_capture`` and captures its rows - in one
        transaction with their events, so an error leaves no row of the call written. The rows as they
        were come from the objects' dirty-field snapshots, else from reading them first, locked; as they
        are after, from reading them after. A stale object isn't such an error: the other rows are
        written and captured, and ``StaleObjectError`` is raised once the transaction commits.

        Args:
            capture_needs: The model's needs.

        Returns:
            How many rows it updated.
        """
        captured = CapturedWrite(
            self.model,
            capture_needs,
            connection,
            RowOperation.UPDATE,
            changed=self._rules.changed_field_names,
        )
        async with captured.transaction() as connection:
            objects = list(self._objects)
            run = BulkUpdateRun(connection, objects)
            pks = [obj.pk for obj in objects]
            before_by_pk: dict[Any, Any] = {}
            if capture_needs.reads_before:
                unread_pks = []
                for obj in objects:
                    snapshot_values = InstanceCapture.get_snapshot_values(obj, capture_needs)
                    if snapshot_values is None:
                        unread_pks.append(obj.pk)
                    else:
                        key = ChangeCapturing.get_stored_pk(obj, connection.dialect.types, obj.pk)
                        before_by_pk[key] = snapshot_values
                if unread_pks:
                    before_by_pk.update(await captured.read_before(unread_pks))
            stale_error: StaleObjectError | None = None
            try:
                count = await self._report_bulk_update(run, self._run(run))
            except StaleObjectError as error:
                stale_error = error
            except BaseException:
                if not connection.features.supports_transactions and run.completed_objects:
                    # No transaction undoes the batches written before the error - their rows are captured.
                    completed_pks = [obj.pk for obj in run.completed_objects if obj not in run.unwritten_objects]
                    await self._capture_rows(captured, completed_pks, before_by_pk)
                raise
            if run.unwritten_objects:
                unwritten_pks = {obj.pk for obj in run.unwritten_objects}
                pks = [pk for pk in pks if pk not in unwritten_pks]
            await self._capture_rows(captured, pks, before_by_pk)
        if stale_error is not None:
            raise stale_error
        return count

    @staticmethod
    async def _capture_rows(captured: CapturedWrite, pks: list[Any], before_by_pk: dict[Any, Any]) -> None:
        """Captures the rows the update wrote - their values read after it.

        Args:
            captured: The capture of the update.
            pks: The keys of the rows written.
            before_by_pk: The rows' captured values before the update, by primary key.
        """
        after_by_pk = await captured.read(pks)
        await captured.capture_values(
            (pk, before_by_pk.get(pk), values, tenant) for pk, (values, tenant) in after_by_pk.items()
        )

    async def _report_bulk_update(self, run: BulkUpdateRun[TModel], update: Awaitable[int | None]) -> int | None:
        """Runs the update and reports its objects' rows (``ChangeEvents``) - also when it fails with
        rows left written: a stale object fails the call after the other rows are written, and
        batches of an explicit ``batch_size`` are statements of their own.

        Args:
            update: The update.

        Returns:
            How many rows it updated.
        """
        try:
            count = await update
        except StaleObjectError:
            await self._report_updated_objects(run)
            raise
        except BaseException:
            if self._batch_size is not None:
                await self._report_updated_objects(run, completed_only=True)
            raise
        await self._report_updated_objects(run)
        return count

    async def _report_updated_objects(self, run: BulkUpdateRun[TModel], *, completed_only: bool = False) -> None:
        """Reports the rows ``bulk_update()`` wrote of the objects it was given (``ChangeEvents``).

        Args:
            completed_only: Whether only the objects of the statements that ran to the end are
                reported - of a call that failed.
        """
        if not ChangeEvents.is_observed(self.model):
            return
        instances = list(run.completed_objects if completed_only else run.objects)
        if not instances:
            return
        if run.unwritten_objects:
            unwritten_object_ids = set(map(id, run.unwritten_objects))
            instances = [obj for obj in instances if id(obj) not in unwritten_object_ids]
        await WriteSteps.report(
            run.connection,
            self.model,
            RowOperation.UPDATE,
            instances=instances,
            fields=self._rules.changed_field_names,
        )

    def _scope_to_active_tenant(self, run: BulkUpdateRun[TModel]) -> None:
        """Checks every object belongs to the model's tenant scope and, for a query outside its
        model's default scope, adds the tenant condition the default scope would - a write reaches
        only the scope's rows whatever an object claims.

        Raises:
            QueryError: The model has no tenant scope, or an object belongs to a tenant outside it.
        """
        if not self._tenant_scoped:
            if self.model._meta.tenant_field:
                # .all_tenants() widens the rows a query reads, not the rows an object points at - an
                # object holding its tenant is checked under the active scope, as save() checks it.
                tenant_field = self.model._meta.tenant_field
                TenantWriteRules.scope_objects(
                    self.model,
                    [obj for obj in run.objects if obj.__dict__.get(tenant_field) is not None],
                    "bulk_update",
                    fills_missing=False,
                    requires_active=False,
                )
            return
        scope = TenantWriteRules.scope_objects(
            self.model, run.objects, "bulk_update", fills_missing=False, requires_active=True
        )
        if not self._uses_default_scope:
            scope_filter = TenantScope.get_scope_filter(self.model._meta.tenant_field, scope)
            if scope_filter is not None:
                run.tenant_condition = Q(**{scope_filter[0]: scope_filter[1]})

    async def _run(self, run: BulkUpdateRun[TModel]) -> int | None:
        # A pending async default is resolved before the values are read, as save() does.
        WrittenTargetChecks.raise_if_refused_calls(self, run.connection.dialect)
        self._scope_to_active_tenant(run)
        for obj in run.objects:
            if obj._await_when_save:
                await InstanceSaving.set_async_default_field(obj)
        await Tenancy.check_objects_relation_targets(self.model, run.objects, run.connection, self.fields)
        # SQL state belongs to a build, bound once to the actual transaction's capabilities.
        query = self.__copy__()
        query._init_build_state()
        query._apply_connection(run.connection)
        query._is_execution_query = True
        return await query._write_checked_objects(run)

    async def _write_checked_objects(self, run: BulkUpdateRun[TModel]) -> int | None:
        """Writes the objects once their relations are checked.

        Returns:
            How many rows it updated.
        """
        if not run.objects:
            return 0
        # The stamps and blind indexes the update gives the objects - an object whose row isn't written
        # gets them put back.
        stamped_values = InstanceValues()
        try:
            self._rules.stamp(run.objects, stamped_values)
            queries = self._make_queries(run)
        except BaseException as error:
            WriteFinalizer.restore_failed_values(stamped_values, error)
            if isinstance(error, (ValidationError, TypeError)):
                self._rules.raise_if_unbound_values(run.objects, self._rules.field_names, error)
            raise
        if len(queries) == 1 or self._batch_size is not None:
            # Explicit batches are independent writes. A later failure cannot undo
            # values already published for completed earlier statements.
            try:
                return await self._execute_many(run, queries, stamped_values)
            except BaseException as error:
                completed_ids = {id(obj) for obj in run.completed_objects}
                WriteFinalizer.restore_failed_values(
                    stamped_values, error, (obj for obj in run.objects if id(obj) not in completed_ids)
                )
                raise
        # Batches split only to respect the parameter ceiling form one transaction.
        try:
            async with run.connection._in_transaction() as transaction_connection:
                count, stale_object_count = await self._execute_statements(
                    run, transaction_connection, queries, stamped_values
                )
        except BaseException as error:
            WriteFinalizer.restore_failed_values(stamped_values, error)
            raise
        # Stale rows do not undo the other rows successfully written by the call.
        self._raise_for_stale_objects(stale_object_count)
        return count

    def _prepare_preview_objects(self) -> tuple[BulkUpdateRun[TModel], tuple[str, ...]]:
        """Inspect copies without computing defaults, timestamps or blind indexes."""
        run = BulkUpdateRun(self._connection, [] if self._is_none else [copy(obj) for obj in self._objects])
        self._scope_to_active_tenant(run)
        deferred = set(self._rules.saved_attribute_names) if run.objects else set()
        for obj in run.objects:
            deferred.update(obj._await_when_save)
        return run, tuple(sorted(deferred))

    def preview(self) -> WritePreview:
        """Show bound batches or explicitly defer values that require execution.

        Explicit batches run independently; implicit splitting at the parameter
        ceiling requires a transaction. Preview evaluates neither stamps nor defaults
        and does not reserve the displayed values for a later execution.
        """
        with Registries.bind():
            query = self._get_execution_query(True)
            run, deferred = query._prepare_preview_objects()
            if deferred:
                return WritePreview(None, None, deferred)
            batches = query._make_queries(run) if run.objects else []
            return WritePreview(
                tuple(batch.command for batch in batches), len(batches) > 1 and query._batch_size is None
            )

    def _get_statements(self, parameters_inline: bool) -> list[tuple[str, list[Any]]]:
        run, deferred = self._prepare_preview_objects()
        if deferred:
            raise QueryError(BULK_UPDATE_DEFERRED_PREVIEW_MESSAGE.format(fields=", ".join(deferred)))
        if not run.objects:
            return []
        return [
            (batch.command.sql, batch.command.parameters)
            for batch in self._make_queries(run, parameters_inline=parameters_inline)
        ]
