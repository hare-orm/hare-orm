from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator, Iterable, Sequence
from itertools import chain, compress
from operator import attrgetter, not_
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from hare.exceptions import (
    StaleObjectError,
)
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.instrumentation.enums import RowOperation
from hare.models.tenancy import Tenancy
from hare.models.write.instance_values import InstanceValues
from hare.models.write.returned_values import ReturnedValues
from hare.models.write.write_fields import WriteFields
from hare.models.write.write_steps import WriteSteps
from hare.query.constants import BULK_UPDATE_MATCHES_FILTER_ALIAS, BULK_UPDATE_VALUES_ALIAS
from hare.query.expressions import Q
from hare.query.queryset.query_spec import QuerySpec
from hare.query.rows.hydrate_accelerator import HydrateAccelerator
from hare.query.rows.value_field import ValueField
from hare.query.scopes.tenant_scope import TenantScope
from hare.query.statements.write.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.update_query import UpdateQuery
from hare.sql.functions.cast import Cast
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.terms.arithmetic.case import Case
from hare.sql.terms.base.literal_value import LiteralValue
from hare.sql.terms.base.parameterizer import Parameterizer
from hare.sql.terms.base.term import Term
from hare.sql.terms.tuple import Tuple

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.base.field import Field
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class BulkUpdateQuery(UpdateQuery, Generic[TModel]):
    __slots__ = ("fields", "_objects", "_batch_size", "_returning", "_filter_criterion", "_tenant_scoped")

    def __init__(
        self,
        source: QuerySpec[Any],
        objects: Iterable[TModel],
        fields: Iterable[str],
        batch_size: int | None = None,
        returning: bool = False,
    ):
        """
        Args:
            source: The queryset whose rows the objects are written among.
            objects: The instances to write.
            fields: The fields to write.
            batch_size: How many instances one statement writes.
            returning: Read the written rows back into the instances.
        """
        super().__init__(source, {})
        self.fields = fields
        self._objects = objects
        self._batch_size = batch_size
        self._returning = returning
        # Whether the rows belong to the tenant active when the query runs (Meta.tenant_field set,
        # no .all_tenants()) - set by QuerySet.bulk_update().
        self._tenant_scoped = False
        # The queryset's own filter as a criterion over the base table alone, set by
        # _make_queries() - None when the queryset carries no filter.
        self._filter_criterion: Term | None = None

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

    def _make_queries(self, params_inline: bool = False) -> list[tuple[str, list[Any], list[TModel]]]:
        # One UPDATE over a VALUES table of the rows - a single pass, instead of a CASE per field.
        # Reset first: a rebuild would take every join as already made.
        self._reset_joined_tables()
        table = self.model._meta.basetable
        # Built as a SELECT first: get_filters() needs somewhere to attach a JOIN, which the
        # hand-built UPDATE ... FROM (VALUES ...) can carry only as a primary key subquery.
        self._apply_ambient_scope()
        self.query = self._get_base_query()
        self.get_filters()
        # A filter on an aggregate annotation needs its GROUP BY to be applied per row.
        self._apply_auto_group_by()
        if self._filters_need_primary_key_subquery():
            pk_attr_names_for_subquery = self.model._meta.pk_attr_names
            pk_columns_for_subquery = [
                self.model._meta.fields_map[name].source_field or name for name in pk_attr_names_for_subquery
            ]
            pk_fields_for_subquery = [table[column] for column in pk_columns_for_subquery]
            # The annotation terms get_filters() selected would become extra subquery columns.
            self.query._selects = []
            subquery = self.query.select(*pk_fields_for_subquery)
            pk_reference = (
                pk_fields_for_subquery[0] if len(pk_fields_for_subquery) == 1 else Tuple(*pk_fields_for_subquery)
            )
            extra_where = pk_reference.isin(subquery)
        else:
            extra_where = self.query._wheres
        self._filter_criterion = extra_where or None
        # Applied after the pk-selecting subquery above is built, so the WITH clause is rendered
        # once, ahead of the hand-built UPDATE, rather than inside that subquery too.
        self._apply_with_ctes()

        # One PK column for a regular model, N (in pk_attr order) for a composite one - everything
        # below is written to that general case, a single-column PK is just N=1 of it.
        pk_attr_names = self.model._meta.pk_attr_names
        pk_field_objs = [self.model._meta.fields_map[name] for name in pk_attr_names]
        source_pk_attrs = [obj.source_field or name for obj, name in zip(pk_field_objs, pk_attr_names)]
        num_pk_cols = len(pk_attr_names)
        # Looked up here (not just below, alongside optimistic_lock_field_obj/version_source_col) because
        # returning_columns (immediately below) already needs to know whether a optimistic_lock_field
        # exists.
        optimistic_lock_field = self.model._meta.optimistic_lock_field
        # An auto_now field stamped on an instance has to be put back when its row wasn't written -
        # known from RETURNING.
        has_auto_now_field = any(
            getattr(self.model._meta.fields_map[field_name], "auto_now", False) for field_name in self.fields
        )
        # RETURNING carries the primary key - the rows come in no order - and, with returning=True,
        # the generated columns. The key alone is returned for a model with an optimistic lock
        # field, dirty tracking or an updated auto_now field: only the objects whose rows were
        # written get their version, baseline and stamps.
        returning_columns = (
            list(dict.fromkeys(source_pk_attrs + list(self.model._meta.generated_db_fields)))
            if self._returning and self.model._meta.generated_db_fields
            else list(source_pk_attrs)
            if optimistic_lock_field or self.model._meta.track_dirty_fields or has_auto_now_field
            else []
        )
        # A relation is written through its key column field(s) - every one of them for a composite
        # key.
        fields: list[str] = []
        for field in self.fields:
            field_obj = self.model._meta.fields_map[field]
            if isinstance(field_obj, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                fields.extend(field_obj.source_fields)
            else:
                fields.append(field)
        # A relation and its own key field named together write the same column once.
        fields = list(dict.fromkeys(fields))
        field_objs = [self.model._meta.fields_map[field] for field in fields]
        dialect = self._db.dialect
        # A database may not infer a column's type from a VALUES row that mixes types across
        # columns - the dialect names the cast every column needs then, the pk column(s) included.
        cast_types = [dialect.get_field_parameter_cast_type(field_obj) for field_obj in field_objs]
        pk_cast_types = [dialect.get_field_parameter_cast_type(obj) for obj in pk_field_objs]
        source_fields = [field_obj.source_field or field for field_obj, field in zip(field_objs, fields)]

        # Optimistic locking: the old version is per object, so it rides in the VALUES row after the
        # key and is matched in WHERE like the key; the bump is the same SET for every row.
        optimistic_lock_field_obj = (
            self.model._meta.fields_map[optimistic_lock_field] if optimistic_lock_field else None
        )
        version_source_col = (
            (optimistic_lock_field_obj.source_field or optimistic_lock_field)
            if optimistic_lock_field_obj and optimistic_lock_field
            else None
        )
        version_cast_type = (
            dialect.get_field_parameter_cast_type(optimistic_lock_field_obj) if optimistic_lock_field_obj else None
        )
        num_version_cols = 1 if optimistic_lock_field else 0

        base_ctx = self.query.QUERY_CLS.SQL_CONTEXT
        quote: Callable[[str], str] = lambda name: base_ctx.quote(name)  # noqa: E731
        db_table = self.model._meta.db_table
        values_alias = BULK_UPDATE_VALUES_ALIAS
        while values_alias == db_table:
            values_alias += "_"
        quoted_values_alias = quote(values_alias)
        # Table-qualified on the right-hand side and in WHERE, so a model column named like one of
        # the VALUES table's own c<N> columns can't be read from the wrong side.
        qualify: Callable[[str], str] = lambda column: f"{quote(db_table)}.{quote(column)}"  # noqa: E731
        # c0..c{num_pk_cols-1}=pk column(s), next=version (if any), the rest=updated fields
        value_col_names = [f"c{i}" for i in range(num_pk_cols + num_version_cols + len(fields))]
        query_class = self._db.query_class
        values_columns_sql = query_class.get_values_table_columns_sql(value_col_names)
        field_value_col_names = value_col_names[num_pk_cols + num_version_cols :]
        assignments = [
            (quote(source_fields[i]), f"{quoted_values_alias}.{field_value_col_names[i]}") for i in range(len(fields))
        ]
        if version_source_col is not None:
            assignments.append((quote(version_source_col), f"{qualify(version_source_col)} + 1"))
        matches = [
            (qualify(source_pk_attrs[i]), f"{quoted_values_alias}.{value_col_names[i]}") for i in range(num_pk_cols)
        ]
        if version_source_col is not None:
            matches.append((qualify(version_source_col), f"{quoted_values_alias}.{value_col_names[num_pk_cols]}"))
        returning = [(qualify(column), quote(column)) for column in returning_columns]

        # The values of the whole chunk come from one serialize_instances() call. One multi-row
        # UPDATE per chunk is one round trip - a statement per object would cost a round trip each.

        # Inlined SQL binds nothing, so only the parameterized path is limited by the bind-parameter
        # ceiling - less what every statement binds besides its rows.
        batch_size = (
            self._batch_size
            if params_inline
            else BulkWriteBatches.get_bind_param_safe_batch_size(
                self._batch_size,
                len(value_col_names),
                self.features.max_bind_parameters - self._get_fixed_parameter_count(base_ctx, extra_where),
            )
        )

        # A value's placeholder depends only on the dialect and the cast type - one template per
        # column, formatted with the parameter index, instead of a Parameter and Cast term per
        # value.
        def placeholder_template(cast_type: str | None) -> str:
            return query_class.get_typed_placeholder_template(dialect.placeholder_template, cast_type)

        pk_placeholder_templates = [placeholder_template(t) for t in pk_cast_types]
        version_placeholder_template = placeholder_template(version_cast_type)
        field_placeholder_templates = [placeholder_template(t) for t in cast_types]
        has_version_col = optimistic_lock_field_obj is not None and optimistic_lock_field is not None
        # pk column(s) + optional version column share one column group (both matched in WHERE
        # by equality, both rendered by the same per-branch logic below) - precomputed once per
        # chunk-building call, not per row, same as every other *_templates/*_cast_types list.
        pk_and_version_placeholder_templates = pk_placeholder_templates + (
            [version_placeholder_template] if has_version_col else []
        )
        pk_and_version_cast_types = pk_cast_types + ([version_cast_type] if has_version_col else [])

        # A whole row - key, old version, fields - is serialized by the field codecs when the key
        # columns write no auto_now stamp (the key is converted without its instance).
        row_fields = [*pk_attr_names, *([optimistic_lock_field] if has_version_col else []), *fields]
        row_placeholder_templates = tuple(pk_and_version_placeholder_templates + field_placeholder_templates)
        serializes_whole_rows = not any(
            getattr(pk_field_obj, "auto_now", False) or getattr(pk_field_obj, "auto_now_add", False)
            for pk_field_obj in pk_field_objs
        )
        queries_with_params: list[tuple[str, list[Any], list[TModel]]] = []
        for objects_item in BulkWriteBatches.get_batches(self._objects, batch_size):
            objects_item = list(objects_item)
            if not objects_item:
                continue
            parameterizer = None if params_inline else Parameterizer()
            vctx = base_ctx if params_inline else base_ctx.copy(parameterizer=parameterizer)
            # The WITH clause is rendered first - its parameters come first.
            with_sql = QueryBuilder._with_sql(self.query._with, vctx) if self.query._with else ""
            if parameterizer is not None and serializes_whole_rows:
                # The key, the old version and the fields of every row in one serialization; the
                # VALUES text depends only on the placeholders and the row count.
                rows = BulkWriteBatches.serialize_instances(
                    self.model, self._db.dialect.types, objects_item, row_fields
                )
                values_sql = BulkWriteBatches.get_values_rows_sql(
                    self.model, row_placeholder_templates, len(parameterizer.values) + 1, len(rows)
                )
                parameterizer.values.extend(chain.from_iterable(rows))
                queries_with_params.append(
                    (
                        query_class.get_update_from_values_sql(
                            with_sql=with_sql,
                            table_sql=table.get_sql(base_ctx),
                            assignments=assignments,
                            values_columns_sql=values_columns_sql,
                            values_sql=values_sql,
                            values_alias_sql=quoted_values_alias,
                            matches=matches,
                            condition_sql=(
                                extra_where.get_sql(vctx.copy(subquery=True, with_namespace=True))
                                if extra_where
                                else None
                            ),
                            returning=returning,
                        ),
                        parameterizer.values,
                        objects_item,
                    )
                )
                continue
            field_rows = BulkWriteBatches.serialize_instances(self.model, self._db.dialect.types, objects_item, fields)
            rows_sql = []
            # Parameterized, each value is appended to the parameter list with its placeholder text;
            # inlined, it is rendered as a literal.
            idx = 0 if parameterizer is None else len(parameterizer.values)
            types = self.dialect.types
            for obj, field_values in zip(objects_item, field_rows, strict=True):
                pk_values = obj.pk if num_pk_cols > 1 else (obj.pk,)
                pk_and_version_db_values = self._pk_and_version_db_values(
                    types, obj, pk_values, pk_field_objs, optimistic_lock_field_obj, optimistic_lock_field
                )
                row_parts = []
                if parameterizer is not None:
                    for db_value, template in zip(
                        pk_and_version_db_values, pk_and_version_placeholder_templates, strict=True
                    ):
                        idx += 1
                        parameterizer.values.append(db_value)
                        row_parts.append(template.format(idx))
                    for field_value, template in zip(field_values, field_placeholder_templates, strict=True):
                        idx += 1
                        parameterizer.values.append(field_value)
                        row_parts.append(template.format(idx))
                else:
                    for db_value, cast_type in zip(pk_and_version_db_values, pk_and_version_cast_types, strict=True):
                        wrapped = self.query._wrapper_cls(db_value)
                        term = Cast(wrapped, cast_type) if cast_type is not None else wrapped
                        row_parts.append(term.get_sql(vctx))
                    for field_value, cast_type in zip(field_values, cast_types, strict=True):
                        wrapped = self.query._wrapper_cls(field_value)
                        term = Cast(wrapped, cast_type) if cast_type is not None else wrapped
                        row_parts.append(term.get_sql(vctx))
                rows_sql.append("(" + ", ".join(row_parts) + ")")
            values_sql = ", ".join(rows_sql)

            sql = query_class.get_update_from_values_sql(
                with_sql=with_sql,
                table_sql=table.get_sql(base_ctx),
                assignments=assignments,
                values_columns_sql=values_columns_sql,
                values_sql=values_sql,
                values_alias_sql=quoted_values_alias,
                matches=matches,
                condition_sql=(
                    extra_where.get_sql(vctx.copy(subquery=True, with_namespace=True)) if extra_where else None
                ),
                returning=returning,
            )
            queries_with_params.append((sql, [] if parameterizer is None else parameterizer.values, objects_item))
        return queries_with_params

    def _get_fixed_parameter_count(self, base_ctx: Any, extra_where: Term | None) -> int:
        """Bind parameters every statement of this call carries besides its VALUES rows.

        Args:
            base_ctx: The dialect's base SQL context.
            extra_where: The queryset's own filter, or None.

        Returns:
            The parameter count of the CTE bodies and the filter.
        """
        parameterizer = Parameterizer()
        fixed_ctx = base_ctx.copy(parameterizer=parameterizer)
        if self.query._with:
            QueryBuilder._with_sql(self.query._with, fixed_ctx)
        if extra_where:
            extra_where.get_sql(fixed_ctx.copy(subquery=True, with_namespace=True))
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
            self._db,
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
        pk_attr_names = self.model._meta.pk_attr_names
        pk_field_objs = [self.model._meta.fields_map[name] for name in pk_attr_names]
        pk_columns = [field_obj.source_field or name for field_obj, name in zip(pk_field_objs, pk_attr_names)]
        table = self.model._meta.basetable
        pk_terms = [table[column] for column in pk_columns]
        pk_values = [obj.pk if len(pk_attr_names) > 1 else (obj.pk,) for obj in not_updated_objects]
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
            pk_condition = Tuple(*pk_terms).isin([Tuple(*db_pk_value) for db_pk_value in db_pk_values])
        matches_filter = (
            Case(alias=BULK_UPDATE_MATCHES_FILTER_ALIAS)
            .when(self._filter_criterion, LiteralValue("1"))
            .else_(LiteralValue("0"))
        )
        query = self._db.query_class.from_(table).select(*pk_terms, matches_filter).where(pk_condition)
        query._with = list(self.query._with)
        _, rows = await self._db.execute(*query.get_parameterized_sql())
        excluded_by_filter_count = sum(1 for row in rows if not int(dict(row)[BULK_UPDATE_MATCHES_FILTER_ALIAS]))
        return len(not_updated_objects) - excluded_by_filter_count

    async def _execute_many(
        self,
        queries_with_params: list[tuple[str, list[Any], list[TModel]]],
        stamped_values: InstanceValues | None = None,
    ) -> int:
        """Runs every statement, then raises for the stale objects found across all of them.

        Args:
            queries_with_params: One ``(sql, values, objects)`` entry per statement.
            stamped_values: The auto_now values serialization stamped on the objects.

        Returns:
            The number of updated rows.

        Raises:
            StaleObjectError: An object had a stale ``Meta.optimistic_lock_field``.
        """
        count, stale_object_count = await self._execute_statements(queries_with_params, stamped_values)
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
        queries_with_params: list[tuple[str, list[Any], list[TModel]]],
        stamped_values: InstanceValues | None = None,
    ) -> tuple[int, int]:
        """Runs every statement of this call, syncing each updated object's in-memory state.

        Args:
            queries_with_params: One ``(sql, values, objects)`` entry per statement.
            stamped_values: The auto_now values serialization stamped on the objects.

        Returns:
            The number of updated rows and the number of stale objects across every statement.
        """
        optimistic_lock_field = self.model._meta.optimistic_lock_field
        track_dirty = self.model._meta.track_dirty_fields
        pk_attr_names = self.model._meta.pk_attr_names
        pk_field_objs = [self.model._meta.fields_map[name] for name in pk_attr_names]
        source_pk_attrs = [obj.source_field or name for obj, name in zip(pk_field_objs, pk_attr_names)]
        populate_generated_fields = bool(self._returning and self.model._meta.generated_db_fields)
        stamped_values = stamped_values or InstanceValues()
        # An object's primary key as _read_returned_keys() gives a row's: a tuple for a composite key.
        get_key = attrgetter(*pk_attr_names)
        count = 0
        # Counted over every chunk - raised once all were attempted.
        total_stale_object_count = 0
        for sql, values, objects_item in queries_with_params:
            rows_affected, returned_rows = await self._db.execute(sql, values)
            stale_object_count = 0
            if optimistic_lock_field or track_dirty or stamped_values.old_values:
                # RETURNING names the rows the UPDATE wrote. Only those objects get their version
                # and dirty baseline updated; any other gets its stamped auto_now values put back.
                updated_pk_values = set(self._read_returned_keys(returned_rows, pk_field_objs, source_pk_attrs))
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
                            obj._register_rollback_restore(
                                self._db,
                                "_dirty_snapshot",
                                dict(obj._dirty_snapshot) if obj._dirty_snapshot is not None else None,
                            )
                            obj._sync_dirty_snapshot_fields(fields_to_sync)
                if stamped_values.old_values:
                    stamped_values.keep(self._db, written_objects)
                    if unwritten_objects:
                        stamped_values.restore(unwritten_objects)
                if optimistic_lock_field and rows_affected < len(objects_item):
                    stale_object_count = await self._count_stale_objects(unwritten_objects)
            if populate_generated_fields:
                # UPDATE ... FROM (VALUES ...) returns its rows in no particular order - matched by
                # primary key; the key itself isn't set again.
                for obj, row in ReturnedValues.match_rows(
                    self.model, self.dialect.types, objects_item, returned_rows, pk_attr_names
                ):
                    ReturnedValues.apply_row(
                        self.model,
                        self.dialect.types,
                        obj,
                        row,
                        [column for column in row if column not in source_pk_attrs],
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
        return query._report_bulk_update(query._run()).__await__()

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
        await WriteSteps.report(
            self._db, self.model, RowOperation.UPDATE, instances=list(self._objects), fields=self.fields
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
                await obj._set_async_default_field()
        await Tenancy.check_objects_relation_targets(self.model, self._objects, self._db, self.fields)
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
        original_db = self._db
        try:
            async with original_db._in_transaction() as transaction_db:
                self._db = transaction_db
                try:
                    count, stale_object_count = await self._execute_statements(queries, stamped_values)
                finally:
                    self._db = original_db
        except BaseException:
            stamped_values.restore()
            raise
        self._raise_for_stale_objects(stale_object_count)
        return count

    def _get_statements(self, params_inline: bool) -> list[tuple[str, list[Any]]]:
        self._objects = list(self._objects)
        self._scope_to_active_tenant()
        return [(sql, values) for sql, values, _objects in self._make_queries(params_inline=params_inline)]
