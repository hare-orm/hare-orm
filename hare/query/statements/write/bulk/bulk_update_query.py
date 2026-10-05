from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator, Iterable, Sequence
from itertools import chain, compress, repeat
from operator import attrgetter, not_
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from hare.exceptions import FieldError, QueryError, StaleObjectError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.instances.instance_saving import InstanceSaving
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.constraints.written_row_checks import WrittenRowChecks
from hare.models.write.instance_capture import InstanceCapture
from hare.models.write.instance_values import InstanceValues
from hare.models.write.returned_values import ReturnedValues
from hare.models.write.rollback_restores import RollbackRestores
from hare.models.write.write_fields import WriteFields
from hare.models.write.write_steps import WriteSteps
from hare.query.expressions import Q
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from hare.query.rows.values_rows.value_field import ValueField
from hare.query.scopes.tenants.tenant_scope import TenantScope
from hare.query.statements.building.query_conditions import QueryConditions
from hare.query.statements.building.query_ctes import QueryCtes
from hare.query.statements.building.query_grouping import QueryGrouping
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.constants import BULK_UPDATE_MATCHES_FILTER_ALIAS, BULK_UPDATE_VALUES_ALIAS
from hare.query.statements.write.bulk.bulk_objects import BulkObjects
from hare.query.statements.write.bulk.bulk_update_checks import BulkUpdateChecks
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.bulk.declarations import BulkUpdateStatementLayout
from hare.query.statements.write.update_query import UpdateQuery
from hare.sql.builder.queries.query_sql_rendering import QuerySqlRendering
from hare.sql.builder.returned_value import ReturnedValue
from hare.sql.functions.cast import Cast
from hare.sql.terms.case.case import Case
from hare.sql.terms.parameters.parameterizer import Parameterizer
from hare.sql.terms.parameters.query_parameters import QueryParameters
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.literal_value import LiteralValue
from hare.sql.terms.values.row_value_list import RowValueList
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.field import Field
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.sql.builder.tables.table import Table
    from hare.sql.sql_context import SqlContext

TModel = TypeVar("TModel", bound="Model")


class BulkUpdateQuery(UpdateQuery, Generic[TModel]):
    __slots__ = ("fields", "_objects", "_batch_size", "_returning", "_filter_criterion", "_tenant_scoped")

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
        model = source.model
        returning = model._meta.returning if returning is None else returning
        objects = list(objects)
        fields = BulkUpdateQuery.get_field_names(model, fields)
        BulkObjects.validate(
            model,
            objects,
            "bulk_update",
            [*BulkUpdateQuery.get_column_field_names(model, fields), *model._meta.primary_key_attribute_names],
        )
        tenant_scoped = bool(model._meta.tenant_field) and not source._visibility.all_tenants
        if tenant_scoped:
            BulkUpdateChecks.check_tenant_values_resolved(model, objects)
        BulkUpdateChecks.check_primary_keys(model, objects)
        BulkUpdateChecks.check_fields(model, fields)
        BulkUpdateChecks.check_values(model, objects, fields)
        fields += BulkUpdateChecks.get_auto_now_field_names(model, objects, fields)
        fields = model._meta.get_with_blind_indexes(fields)
        if source._uses_default_scope:
            # The objects are matched by their keys, like save() matches one: under the default
            # scope a soft-deleted object would match no row. Their tenant is checked above.
            source = source._clone()
            source._uses_default_scope = False
        super().__init__(source, {})
        self.fields = fields
        self._objects = objects
        self._batch_size = batch_size
        self._returning = returning
        # Whether the rows belong to the tenant active when the query runs (Meta.tenant_field set,
        # no .all_tenants()).
        self._tenant_scoped = tenant_scoped
        # The queryset's own filter as a criterion over the base table alone, set by
        # _make_queries() - None when the queryset carries no filter.
        self._filter_criterion: Term | None = None

    @staticmethod
    def get_column_field_names(model: type[Model], field_names: list[str]) -> list[str]:
        """The attributes ``bulk_update()`` reads for ``field_names`` - a forward FK/O2O relation
        is read through its key field(s).

        Args:
            model: The model updated.
            field_names: Validated field names.

        Returns:
            The attribute names.
        """
        column_field_names: list[str] = []
        for field_name in field_names:
            field_object = model._meta.fields_map[field_name]
            relation_source_fields = getattr(field_object, "source_fields", None)
            if relation_source_fields and field_name not in model._meta.fields_db_projection:
                column_field_names.extend(relation_source_fields)
            else:
                column_field_names.append(field_name)
        return column_field_names

    @staticmethod
    def get_field_names(model: type[Model], fields: Iterable[str]) -> list[str]:
        """Validates ``bulk_update()``'s ``fields`` argument.

        Args:
            model: The model updated.
            fields: The field names as the caller passed them.

        Returns:
            The field names, duplicates dropped, in the given order.

        Raises:
            QueryError: ``fields`` is a string or empty, or names the primary key.
            FieldError: A name isn't a field of the model.
        """
        if isinstance(fields, (str, bytes)):
            raise QueryError(
                f"bulk_update() on {model.__name__}: fields must be a list of field names, got the "
                f"string {fields!r} - wrap a single name in a list"
            )
        meta = model._meta
        field_names = list(dict.fromkeys(GenericForeignKeys.expand_field_names(meta, fields)))
        if not field_names:
            raise QueryError(f"bulk_update() on {model.__name__} needs at least one field to update")
        unknown_field_names = [
            field_name for field_name in field_names if field_name != "pk" and field_name not in meta.fields_map
        ]
        if unknown_field_names:
            raise FieldError(
                f"bulk_update() on {model.__name__} got unknown field(s) {unknown_field_names} - "
                f"available fields: {sorted(meta.fields_map)}"
            )
        primary_key_field_names = {"pk", *meta.primary_key_attribute_names}
        for field_name in field_names:
            field_object = meta.fields_map.get(field_name)
            relation_source_fields = getattr(field_object, "source_fields", None) or ()
            if field_name in primary_key_field_names or primary_key_field_names.intersection(relation_source_fields):
                raise QueryError(
                    f"bulk_update() on {model.__name__} can't update the primary key ('{field_name}') - "
                    "every row is matched by it; update it with QuerySet.update() instead"
                )
        return field_names

    @staticmethod
    def _pk_and_version_db_values(
        types: TypeRegistry,
        obj: TModel,
        pk_values: tuple[Any, ...],
        pk_field_objs: list[Any],
        optimistic_lock_field_obj: Any,
        optimistic_lock_field: str | None,
    ) -> list[Any]:
        """The DB values of an object's primary key column(s), then the optimistic lock column's old
        value when the model has one - the column order of a VALUES row.
        """
        db_values = [
            types.get_db_value(pk_field_obj, pk_value, None)
            for pk_value, pk_field_obj in zip(pk_values, pk_field_objs, strict=True)
        ]
        if optimistic_lock_field_obj is not None and optimistic_lock_field is not None:
            db_values.append(types.get_db_value(optimistic_lock_field_obj, getattr(obj, optimistic_lock_field), obj))
        return db_values

    def _make_queries(self, parameters_inline: bool = False) -> list[tuple[str, list[Any], list[TModel]]]:
        """One UPDATE over a VALUES table of the rows per batch of objects - a single pass, instead of
        a CASE per field. One multi-row UPDATE per batch is one round trip.

        Args:
            parameters_inline: Write every value into the SQL text instead of binding it.

        Returns:
            Each statement, its parameters and the objects it writes.
        """
        layout = self._get_statement_layout(parameters_inline=parameters_inline)
        queries_with_parameters: list[tuple[str, list[Any], list[TModel]]] = []
        for batch in BulkWriteBatches.get_batches(self._objects, layout.batch_size):
            objects_item = list(batch)
            if not objects_item:
                continue
            if not parameters_inline and layout.serializes_whole_rows:
                queries_with_parameters.append(self._make_whole_rows_query(layout, objects_item))
            else:
                queries_with_parameters.append(
                    self._make_query_by_value(layout, objects_item, parameters_inline=parameters_inline)
                )
        return queries_with_parameters

    def _get_filter_criterion(self, table: Table) -> Term | None:
        """The queryset's own filter as a criterion over the base table alone - through a primary
        key subquery where it needs a JOIN, which the hand-built ``UPDATE ... FROM (VALUES ...)``
        can't carry. The query's CTEs are applied after it, so the WITH clause is rendered once,
        ahead of the UPDATE, rather than inside that subquery too.

        Args:
            table: The model's table.

        Returns:
            The criterion, None when the queryset carries no filter.
        """
        # Reset first: a rebuild would take every join as already made.
        QueryJoins.reset_joined_tables(self)
        # Built as a SELECT first: QueryConditions.get_filters() needs somewhere to attach a JOIN.
        self._apply_ambient_scope()
        self.query = self._get_base_query()
        QueryConditions.get_filters(self)
        # A filter on an aggregate annotation needs its GROUP BY to be applied per row.
        QueryGrouping.apply_auto_group_by(self)
        if self._filters_need_primary_key_subquery():
            pk_fields_for_subquery = [
                table[self.model._meta.fields_map[name].source_field or name]
                for name in self.model._meta.primary_key_attribute_names
            ]
            # The annotation terms QueryConditions.get_filters() selected would become extra subquery columns.
            self.query._selects = []
            subquery = self.query.select(*pk_fields_for_subquery)
            pk_reference = (
                pk_fields_for_subquery[0] if len(pk_fields_for_subquery) == 1 else Tuple(*pk_fields_for_subquery)
            )
            filter_criterion: Term | None = pk_reference.isin(subquery)
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

    def _get_statement_layout(self, *, parameters_inline: bool) -> BulkUpdateStatementLayout:
        """What every statement of this call shares.

        Args:
            parameters_inline: Whether every value is written into the SQL text.

        Returns:
            The layout.
        """
        meta = self.model._meta
        table = meta.basetable
        filter_criterion = self._filter_criterion = self._get_filter_criterion(table)
        # One PK column for a regular model, N (in primary_key_attribute order) for a composite one -
        # everything below is written to that general case, a single-column PK is just N=1 of it.
        primary_key_attribute_names = meta.primary_key_attribute_names
        pk_field_objects = [meta.fields_map[name] for name in primary_key_attribute_names]
        pk_columns = [
            field_object.source_field or name
            for field_object, name in zip(pk_field_objects, primary_key_attribute_names, strict=True)
        ]
        pk_column_count = len(primary_key_attribute_names)
        fields = self._get_written_column_fields()
        field_objects = [meta.fields_map[field] for field in fields]
        dialect = self._connection.dialect
        get_cast_type = dialect.parameters.get_field_parameter_cast_type
        # A database may not infer a column's type from a VALUES row that mixes types across
        # columns - the dialect names the cast every column needs then, the pk column(s) included.
        field_cast_types = [get_cast_type(field_object) for field_object in field_objects]
        # Optimistic locking: the old version is per object, so it rides in the VALUES row after the
        # key and is matched in WHERE like the key; the bump is the same SET for every row.
        optimistic_lock_field = meta.optimistic_lock_field
        optimistic_lock_field_object = meta.fields_map[optimistic_lock_field] if optimistic_lock_field else None
        key_cast_types = [get_cast_type(field_object) for field_object in pk_field_objects]
        key_fields = list(primary_key_attribute_names)
        version_column = None
        if optimistic_lock_field_object is not None and optimistic_lock_field:
            version_column = optimistic_lock_field_object.source_field or optimistic_lock_field
            key_cast_types.append(get_cast_type(optimistic_lock_field_object))
            key_fields.append(optimistic_lock_field)

        base_context = self.query.query_class.SQL_CONTEXT
        quote: Callable[[str], str] = base_context.quote
        db_table = meta.db_table
        values_alias = BULK_UPDATE_VALUES_ALIAS
        while values_alias == db_table:
            values_alias += "_"
        values_alias_sql = quote(values_alias)
        # c0..c{key column count - 1} = the key column(s) and the old version, the rest the written
        # fields.
        value_column_names = [f"c{column_position}" for column_position in range(len(key_fields) + len(fields))]
        clauses = self._connection.dialect.clauses
        # Table-qualified on the right-hand side and in WHERE, so a model column named like one of
        # the VALUES table's own c<N> columns can't be read from the wrong side.
        table_sql = quote(db_table)
        assignments = [
            (quote(field_object.source_field or field), f"{values_alias_sql}.{value_column_name}")
            for field_object, field, value_column_name in zip(
                field_objects, fields, value_column_names[len(key_fields) :], strict=True
            )
        ]
        matches = [
            (f"{table_sql}.{quote(column)}", f"{values_alias_sql}.{value_column_name}")
            for column, value_column_name in zip(
                [*pk_columns, *([version_column] if version_column is not None else [])],
                value_column_names,
                strict=False,
            )
        ]
        if version_column is not None:
            assignments.append((quote(version_column), f"{table_sql}.{quote(version_column)} + 1"))
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
                self.features.max_bind_parameters - self._get_fixed_parameter_count(base_context, filter_criterion),
            )
        )
        return BulkUpdateStatementLayout(
            table=table,
            base_context=base_context,
            filter_criterion=filter_criterion,
            assignments=assignments,
            matches=matches,
            returned=[
                ReturnedValue(sql=f"{table_sql}.{quote(column)}", alias_sql=quote(column))
                for column in self._get_returning_columns(pk_columns)
            ],
            values_columns_sql=clauses.get_values_table_columns_sql(value_column_names),
            values_alias_sql=values_alias_sql,
            fields=fields,
            field_cast_types=field_cast_types,
            field_placeholder_templates=field_placeholder_templates,
            pk_column_count=pk_column_count,
            pk_field_objects=pk_field_objects,
            optimistic_lock_field=optimistic_lock_field if version_column is not None else None,
            optimistic_lock_field_object=optimistic_lock_field_object if version_column is not None else None,
            key_cast_types=key_cast_types,
            key_placeholder_templates=key_placeholder_templates,
            row_fields=[*key_fields, *fields],
            row_placeholder_templates=tuple(key_placeholder_templates + field_placeholder_templates),
            # A whole row - key, old version, fields - is serialized by the field codecs when the key
            # columns write no auto_now stamp (the key is converted without its instance).
            serializes_whole_rows=not any(
                getattr(field_object, "auto_now", False) or getattr(field_object, "auto_now_add", False)
                for field_object in pk_field_objects
            ),
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
        filter_criterion = layout.filter_criterion
        return self._connection.dialect.clauses.get_update_from_values_sql(
            with_sql=with_sql,
            table=layout.table,
            table_sql=layout.table.get_sql(layout.base_context),
            assignments=layout.assignments,
            values_columns_sql=layout.values_columns_sql,
            values_sql=values_sql,
            values_alias_sql=layout.values_alias_sql,
            matches=layout.matches,
            condition_sql=(
                filter_criterion.get_sql(values_context.copy(subquery=True, with_namespace=True))
                if filter_criterion
                else None
            ),
            returned=layout.returned,
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
    ) -> tuple[str, list[Any], list[TModel]]:
        """The UPDATE of one batch whose rows the field codecs serialize whole - the key, the old
        version and the fields of every row in one serialization; the VALUES text depends only on
        the placeholders and the row count.

        Args:
            layout: What every statement of this call shares.
            objects_item: The batch's objects.

        Returns:
            The statement, its parameters and the objects.
        """
        parameterizer = Parameterizer()
        values_context = layout.base_context.copy(parameterizer=parameterizer)
        # The WITH clause is rendered first - its parameters come first.
        with_sql = QuerySqlRendering.with_sql(self.query._with, values_context) if self.query._with else ""
        row_fields = layout.row_fields
        values_before_rows = len(parameterizer.values)
        parameters = BulkWriteBatches.write_statement_parameters(
            self.model, self._connection, objects_item, row_fields, list(parameterizer.values)
        )
        rows = (
            BulkWriteBatches.serialize_instances(self.model, self._connection.dialect.types, objects_item, row_fields)
            if parameters is None
            else None
        )
        row_count = len(objects_item) if rows is None else len(rows)
        values_sql = BulkWriteBatches.get_values_rows_sql(
            self.model, layout.row_placeholder_templates, values_before_rows + 1, row_count
        )
        if rows is None:
            # The rows' places in the numbering - their values come from the parameters.
            parameterizer.values.extend(repeat(None, row_count * len(row_fields)))
        else:
            parameterizer.values.extend(chain.from_iterable(rows))
        update_sql = self._get_update_sql(layout, values_context, with_sql, values_sql)
        if parameters is not None:
            values_after_rows = parameterizer.values[values_before_rows + row_count * len(row_fields) :]
            return (
                update_sql,
                parameters.extended(values_after_rows) if values_after_rows else parameters,
                objects_item,
            )
        statement_values = self._get_hidden_values(layout, parameterizer.values, values_before_rows, row_count)
        return update_sql, statement_values, objects_item

    def _make_query_by_value(
        self, layout: BulkUpdateStatementLayout, objects_item: list[TModel], *, parameters_inline: bool
    ) -> tuple[str, list[Any], list[TModel]]:
        """The UPDATE of one batch built value by value - parameterized, each value is appended to the
        parameters with its placeholder text; inlined, it is rendered as a literal.

        Args:
            layout: What every statement of this call shares.
            objects_item: The batch's objects.
            parameters_inline: Whether every value is written into the SQL text.

        Returns:
            The statement, its parameters and the objects.
        """
        parameterizer = None if parameters_inline else Parameterizer()
        values_context = (
            layout.base_context if parameterizer is None else layout.base_context.copy(parameterizer=parameterizer)
        )
        # The WITH clause is rendered first - its parameters come first.
        with_sql = QuerySqlRendering.with_sql(self.query._with, values_context) if self.query._with else ""
        field_rows = BulkWriteBatches.serialize_instances(
            self.model, self._connection.dialect.types, objects_item, layout.fields
        )
        rows_sql = []
        parameter_offset = 0 if parameterizer is None else len(parameterizer.values)
        # Each row's key, old version and fields follow the values bound before the rows.
        first_row_value = parameter_offset
        types = self.dialect.types
        for obj, field_values in zip(objects_item, field_rows, strict=True):
            pk_values = obj.pk if layout.pk_column_count > 1 else (obj.pk,)
            key_values = self._pk_and_version_db_values(
                types,
                obj,
                pk_values,
                layout.pk_field_objects,
                layout.optimistic_lock_field_object,
                layout.optimistic_lock_field,
            )
            row_parts = []
            if parameterizer is not None:
                for value, template in chain(
                    zip(key_values, layout.key_placeholder_templates, strict=True),
                    zip(field_values, layout.field_placeholder_templates, strict=True),
                ):
                    parameter_offset += 1
                    parameterizer.values.append(value)
                    row_parts.append(template.format(parameter_offset))
            else:
                for value, cast_type in chain(
                    zip(key_values, layout.key_cast_types, strict=True),
                    zip(field_values, layout.field_cast_types, strict=True),
                ):
                    wrapped = self.query._wrapper_class(value)
                    term = Cast(wrapped, cast_type) if cast_type is not None else wrapped
                    row_parts.append(term.get_sql(values_context))
            rows_sql.append("(" + ", ".join(row_parts) + ")")
        update_sql = self._get_update_sql(layout, values_context, with_sql, ", ".join(rows_sql))
        if parameterizer is None:
            return update_sql, [], objects_item
        statement_values = self._get_hidden_values(layout, parameterizer.values, first_row_value, len(objects_item))
        return update_sql, statement_values, objects_item

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

    def _read_returned_keys(self, rows: Sequence[Any], pk_fields: list[Field[Any]], columns: list[str]) -> list[Any]:
        """The primary key of each ``RETURNING`` row, as the objects hold it.

        Args:
            rows: The rows, the key's columns first.
            pk_fields: The primary key's fields.
            columns: The primary key's columns.

        Returns:
            One key per row - a tuple for a composite key.
        """
        types = self.dialect.types
        is_composite = len(pk_fields) > 1

        def read_in_python() -> list[Any]:
            # A sqlite3.Row has no .get() - read as a dict like a PostgreSQL row.
            keys = [
                tuple(
                    types.get_python_value(field, row.get(column))
                    for field, column in zip(pk_fields, columns, strict=True)
                )
                for row in map(dict, rows)
            ]
            return keys if is_composite else [key for (key,) in keys]

        if HydrateAccelerator.module is None or not rows:
            return read_in_python()
        reader = HydrateAccelerator.get_values_reader(
            self.model,
            tuple(ValueField(self.model, field.model_field_name, field, False) for field in pk_fields),
            self._connection,
        )
        read = reader.read_tuples if is_composite else reader.read_flat
        return cast("list[Any]", HydrateAccelerator.run_or_fall_back(lambda: read(list(rows)), read_in_python))

    async def _count_stale_objects(self, not_updated_objects: list[TModel]) -> int:
        """Counts the objects whose row the UPDATE skipped for a stale ``Meta.optimistic_lock_field``,
        rather than for the queryset's own filter.

        Args:
            not_updated_objects: Objects of one batch whose row the UPDATE didn't touch.

        Returns:
            How many of them are stale - a row that no longer exists counts too.
        """
        if not not_updated_objects or self._filter_criterion is None:
            return len(not_updated_objects)
        primary_key_attribute_names = self.model._meta.primary_key_attribute_names
        pk_field_objs = [self.model._meta.fields_map[name] for name in primary_key_attribute_names]
        pk_columns = [
            field_obj.source_field or name
            for field_obj, name in zip(pk_field_objs, primary_key_attribute_names, strict=True)
        ]
        table = self.model._meta.basetable
        pk_terms = [table[column] for column in pk_columns]
        pk_values = [obj.pk if len(primary_key_attribute_names) > 1 else (obj.pk,) for obj in not_updated_objects]
        db_pk_values = [
            [
                self.dialect.types.get_db_value(field_obj, value, None)
                for field_obj, value in zip(pk_field_objs, pk_value, strict=True)
            ]
            for pk_value in pk_values
        ]
        if len(pk_terms) == 1:
            pk_condition = pk_terms[0].isin([db_pk_value[0] for db_pk_value in db_pk_values])
        else:
            pk_condition = Tuple(*pk_terms).isin(RowValueList(*[Tuple(*db_pk_value) for db_pk_value in db_pk_values]))
        matches_filter = (
            Case(alias=BULK_UPDATE_MATCHES_FILTER_ALIAS)
            .when(self._filter_criterion, LiteralValue("1"))
            .else_(LiteralValue("0"))
        )
        query = self._connection.query_class.from_(table).select(*pk_terms, matches_filter).where(pk_condition)
        query._with = list(self.query._with)
        _, rows = await self._connection.execute(*query.get_parameterized_sql())
        excluded_by_filter_count = sum(1 for row in rows if not int(dict(row)[BULK_UPDATE_MATCHES_FILTER_ALIAS]))
        return len(not_updated_objects) - excluded_by_filter_count

    async def _execute_many(
        self,
        queries_with_parameters: list[tuple[str, list[Any], list[TModel]]],
        stamped_values: InstanceValues | None = None,
    ) -> int:
        """Runs every statement, then raises for the stale objects found across all of them.

        Args:
            queries_with_parameters: One ``(sql, values, objects)`` entry per statement.
            stamped_values: The auto_now values serialization stamped on the objects.

        Returns:
            The number of updated rows.

        Raises:
            StaleObjectError: An object had a stale ``Meta.optimistic_lock_field``.
        """
        count, stale_object_count = await self._execute_statements(queries_with_parameters, stamped_values)
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
        queries_with_parameters: list[tuple[str, list[Any], list[TModel]]],
        stamped_values: InstanceValues | None = None,
    ) -> tuple[int, int]:
        """Runs every statement of this call, syncing each updated object's in-memory state.

        Args:
            queries_with_parameters: One ``(sql, values, objects)`` entry per statement.
            stamped_values: The auto_now values serialization stamped on the objects.

        Returns:
            The number of updated rows and the number of stale objects across every statement.
        """
        optimistic_lock_field = self.model._meta.optimistic_lock_field
        track_dirty = self.model._meta.track_dirty_fields
        primary_key_attribute_names = self.model._meta.primary_key_attribute_names
        pk_field_objs = [self.model._meta.fields_map[name] for name in primary_key_attribute_names]
        source_primary_key_attributes = [
            obj.source_field or name for obj, name in zip(pk_field_objs, primary_key_attribute_names, strict=True)
        ]
        populate_generated_fields = bool(self._returning and self.model._meta.generated_db_fields)
        # Told to the connection - a statement of many rows is long to search for its RETURNING.
        returns_rows = populate_generated_fields or self._reads_written_keys()
        stamped_values = stamped_values or InstanceValues()
        # An object's primary key as _read_returned_keys() gives a row's: a tuple for a composite key.
        get_key = attrgetter(*primary_key_attribute_names)
        count = 0
        # Counted over every chunk - raised once all were attempted.
        total_stale_object_count = 0
        for sql, values, objects_item in queries_with_parameters:
            rows_affected, returned_rows = await self._connection.execute(sql, values, returns_rows=returns_rows)
            stale_object_count = 0
            if optimistic_lock_field or track_dirty or stamped_values.old_values:
                # RETURNING names the rows the UPDATE wrote. Only those objects get their version
                # and dirty baseline updated; any other gets its stamped auto_now values put back.
                updated_pk_values = set(
                    self._read_returned_keys(returned_rows, pk_field_objs, source_primary_key_attributes)
                )
                # Only the written fields become clean - other unsaved changes of the object stay
                # dirty.
                fields_to_sync = WriteFields.of(self.model).get_written_with(self.fields)
                is_written = list(map(updated_pk_values.__contains__, map(get_key, objects_item)))
                written_objects = list(compress(objects_item, is_written))
                unwritten_objects = list(compress(objects_item, map(not_, is_written)))
                if optimistic_lock_field or track_dirty:
                    for obj in written_objects:
                        # The version bump is registered to be put back if the transaction rolls back.
                        if optimistic_lock_field:
                            stamped_values.bump_optimistic_lock(obj, optimistic_lock_field)
                        if track_dirty:
                            RollbackRestores.register_rollback_restore(
                                obj,
                                self._connection,
                                "_dirty_snapshot",
                                dict(obj._dirty_snapshot) if obj._dirty_snapshot is not None else None,
                            )
                            DirtyFields.sync_dirty_snapshot_fields(obj, fields_to_sync)
                if stamped_values.old_values:
                    stamped_values.keep(self._connection, written_objects)
                    if unwritten_objects:
                        stamped_values.restore(unwritten_objects)
                if optimistic_lock_field and rows_affected < len(objects_item):
                    stale_object_count = await self._count_stale_objects(unwritten_objects)
            if populate_generated_fields:
                # UPDATE ... FROM (VALUES ...) returns its rows in no particular order - matched by
                # primary key; the key itself isn't set again.
                for obj, row in ReturnedValues.match_rows(
                    self.model, self.dialect.types, objects_item, returned_rows, primary_key_attribute_names
                ):
                    ReturnedValues.apply_row(
                        self.model,
                        self.dialect.types,
                        obj,
                        row,
                        [column for column in row if column not in source_primary_key_attributes],
                    )
            if stale_object_count:
                # Every chunk is its own UPDATE - a stale object in one doesn't stop the others;
                # raised after the loop.
                total_stale_object_count += stale_object_count
            count += rows_affected
        return count, total_stale_object_count

    def __await__(self) -> Generator[Any, Any, int]:
        if self._is_none:
            return self._execute_none().__await__()
        query = self._get_execution_query(True)
        capture_needs = query.model._meta.change_capture_needs
        if capture_needs is not None and capture_needs.captures(RowOperation.UPDATE):
            return query._run_captured_bulk_update(capture_needs).__await__()
        return query._report_bulk_update(query._run()).__await__()

    async def _run_captured_bulk_update(self, capture_needs: CaptureNeeds) -> int:
        """Runs the update of a model with ``Meta.change_capture`` and captures its rows - in one
        transaction, so a stale object leaves no row of the call written. The rows as they were come
        from the objects' dirty-field snapshots, else from reading them first, locked; as they are
        after, from reading them after.

        Args:
            capture_needs: The model's needs.

        Returns:
            How many rows it updated.
        """
        async with ChangeCapturing.transaction(self._connection) as connection:
            if connection is not self._connection:
                self._apply_connection(connection)
                self._connection_explicitly_chosen = True
            objects = list(self._objects)
            self._objects = objects
            pks = [obj.pk for obj in objects]
            before_by_pk: dict[Any, Any] = {}
            if capture_needs.reads_before:
                unread_pks = []
                for obj in objects:
                    snapshot_values = InstanceCapture.get_snapshot_values(obj, capture_needs)
                    if snapshot_values is None:
                        unread_pks.append(obj.pk)
                    else:
                        before_by_pk[obj.pk] = snapshot_values
                if unread_pks:
                    values_by_pk = await ChangeCapturing.read_values(
                        self.model, connection, unread_pks, capture_needs, lock=True
                    )
                    before_by_pk.update((pk, values) for pk, (values, _tenant) in values_by_pk.items())
            count = await self._report_bulk_update(self._run())
            after_by_pk = await ChangeCapturing.read_values(self.model, connection, pks, capture_needs)
            occurred_at = Timezone.now()
            changes = [
                ChangeCapturing.build_change(
                    self.model,
                    capture_needs,
                    RowOperation.UPDATE,
                    pk,
                    changed=self.fields,
                    before=before_by_pk.get(pk),
                    after=values,
                    tenant=tenant,
                    occurred_at=occurred_at,
                )
                for pk, (values, tenant) in after_by_pk.items()
            ]
            await ChangeCapturing.capture(connection, self.model, changes)
        return count

    async def _report_bulk_update(self, update: Awaitable[int]) -> int:
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
            await self._report_updated_objects()
            raise
        except BaseException:
            if self._batch_size is not None:
                await self._report_updated_objects()
            raise
        await self._report_updated_objects()
        return count

    async def _report_updated_objects(self) -> None:
        """Reports the rows of the objects ``bulk_update()`` was given (``ChangeEvents``)."""
        if not ChangeEvents.is_observed(self.model):
            return
        await WriteSteps.report(
            self._connection, self.model, RowOperation.UPDATE, instances=list(self._objects), fields=self.fields
        )

    def _scope_to_active_tenant(self) -> None:
        """Checks every object belongs to the model's tenant scope and, for a query outside its
        model's default scope, adds the tenant condition the default scope would - a write reaches
        only the scope's rows whatever an object claims.

        Raises:
            QueryError: The model has no tenant scope, or an object belongs to a tenant outside it.
        """
        if not self._tenant_scoped:
            return
        scope = WriteSteps.scope_to_active_tenant(
            self.model, self._objects, "bulk_update", fills_missing=False, requires_active=True
        )
        if not self._uses_default_scope:
            scope_filter = TenantScope.get_scope_filter(cast("str", self.model._meta.tenant_field), scope)
            if scope_filter is not None:
                self._q_objects = [*self._q_objects, Q(**{scope_filter[0]: scope_filter[1]})]

    async def _run(self) -> int:
        # A pending async default is resolved before the values are read, as save() does.
        self._objects = list(self._objects)
        self._scope_to_active_tenant()
        for obj in self._objects:
            if obj._await_when_save:
                await InstanceSaving.set_async_default_field(obj)
        await Tenancy.check_objects_relation_targets(self.model, self._objects, self._connection, self.fields)
        await WrittenRowChecks.check_changed_rows(self.model, self._connection, self._objects, self.fields)
        # Captured before the objects are serialized: to_db_value() stamps auto_now fields on the
        # instance, and an object whose row isn't written gets them put back.
        stamped_values = InstanceValues()
        auto_now_names = WriteFields.of(self.model).auto_now_names
        stamped_values.capture(self._objects, [name for name in self.fields if name in auto_now_names])
        queries = self._make_queries()
        if len(queries) == 1 or self._batch_size is not None:
            # One statement is atomic on its own; batches the caller asked for explicitly run as
            # independent statements, as documented.
            return await self._execute_many(queries, stamped_values)
        # Batches split off only to stay under the bind-parameter ceiling run in one transaction,
        # so a failure in any of them leaves no row updated - a stale version is not a failure
        # and doesn't roll back the other rows, exactly as with a single statement.
        original_connection = self._connection
        try:
            async with original_connection._in_transaction() as transaction_connection:
                self._connection = transaction_connection
                try:
                    count, stale_object_count = await self._execute_statements(queries, stamped_values)
                finally:
                    self._connection = original_connection
        except BaseException:
            stamped_values.restore()
            raise
        self._raise_for_stale_objects(stale_object_count)
        return count

    def _get_statements(self, parameters_inline: bool) -> list[tuple[str, list[Any]]]:
        self._objects = list(self._objects)
        self._scope_to_active_tenant()
        return [(sql, values) for sql, values, _objects in self._make_queries(parameters_inline=parameters_inline)]
