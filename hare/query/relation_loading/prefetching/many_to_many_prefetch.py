from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from functools import partial
from operator import attrgetter
from typing import TYPE_CHECKING, Any

from hare.dialects.base.constants import (
    PREFETCH_MAX_SLICED_COMPOSITE_PARENTS,
)
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models.instances.instance_connections import InstanceConnections
from hare.native.native_modules import NativeModules
from hare.query.execution.query_execution import QueryExecution
from hare.query.expressions import F, Q
from hare.query.filters.lookups.value_encoders import ValueEncoders
from hare.query.key_columns import KeyColumns
from hare.query.plans.statement.statement_plan_runs import StatementPlanRuns
from hare.query.relation_loading.constants import PREFETCH_PARENT_KEY_ANNOTATION, PREFETCH_ROW_NUMBER_ANNOTATION
from hare.query.relation_loading.prefetching.declarations import ManyToManyPrefetchJob, PrefetchNode
from hare.query.relation_loading.prefetching.enums import PrefetchRowsStrategy
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks
from hare.query.relation_loading.prefetching.prefetch_keys import PrefetchKeys
from hare.query.relation_loading.prefetching.related_rows_fetch import RelatedRowsFetch
from hare.query.relation_loading.prefetching.sliced_prefetch import SlicedPrefetch
from hare.query.statements.select.grouped_model_rows_query import GroupedModelRowsQuery
from hare.sql.builder.tables.table import Table

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset import QuerySet
    from hare.query.relation_loading.prefetching.prefetch_result import PrefetchResult
    from hare.query.relation_loading.prefetching.prefetcher import Prefetcher


class ManyToManyPrefetch:
    """The prefetch of a many-to-many relation: the related rows read through the through table -
    joined in one query where possible, otherwise fetched separately with key batches as needed.
    Requested slices are applied per parent before attachments are collected."""

    @staticmethod
    async def prefetch_many_to_many_relation(
        prefetcher: Prefetcher,
        objs: Iterable[Model],
        node: PrefetchNode,
        result: PrefetchResult,
    ) -> None:
        """Fetch the related rows and collect detached many-to-many attachments.

        Args:
            prefetcher: The prefetcher.
            objs: The objs whose relation is prefetched.
            node: The bound relation, clients, queryset and selected strategy.
            result: Detached assignments collected for publication after all fetches.
        """
        field, to_attribute, related_queryset = node.field, node.to_attribute, node.queryset
        # PrefetchPlan supplies both resources for every many-to-many fetch node.
        assert related_queryset is not None and node.through_connection is not None  # nosec B101
        objs = list(objs)

        field_object: ManyToManyFieldInstance[Any] = prefetcher.model._meta.fields_map[field]  # type: ignore[assignment]
        PrefetchChecks.reject_many_to_many_prefetch_queryset_model_mismatch(related_queryset, field, field_object)
        job = ManyToManyPrefetchJob(
            prefetcher,
            objs,
            field,
            field_object,
            to_attribute,
            related_queryset,
            result,
            node.through_connection,
            node.rows_strategy,
        )
        if node.rows_strategy is PrefetchRowsStrategy.NUMBERED:
            return await ManyToManyPrefetch.prefetch_sliced_many_to_many_relation(job)
        parent_meta = prefetcher.model._meta
        target_meta = related_queryset.model._meta
        through_connection = node.through_connection
        if node.rows_strategy is PrefetchRowsStrategy.JOIN:
            parent_key_values = list(dict.fromkeys(map(attrgetter(parent_meta.primary_key_attribute_names[0]), objs)))
            return await ManyToManyPrefetch.prefetch_many_to_many_by_join(job, parent_key_values)
        # The through table is read first, then the related rows by primary key - the same for a
        # single-column and a composite key on either side.
        # The through rows are read by position where rust.native.rows lays them out - single-column
        # keys on a driver giving positional rows - by column name otherwise.
        reads_natively = (
            through_connection.features.supports_positional_rows
            and len(parent_meta.pk_fields) == 1
            and len(target_meta.pk_fields) == 1
        )
        through_rows = await ManyToManyPrefetch.read_through_rows(
            job, through_connection, reads_natively=reads_natively
        )
        if reads_natively:
            return await ManyToManyPrefetch.lay_out_many_to_many_rows_natively(job, through_connection, through_rows)
        return await ManyToManyPrefetch.lay_out_many_to_many_rows(job, through_connection, through_rows)

    @staticmethod
    def get_through_connection(prefetcher: Prefetcher, field_object: ManyToManyFieldInstance[Any]) -> DatabaseClient:
        """The connection a relation's through table is read on - the owning side's: the owning
        model's own when it differs or a router decides, else the one the parent rows were read on.

        Args:
            prefetcher: The prefetcher.
            field_object: The many-to-many field.

        Returns:
            The connection.
        """
        through_model = field_object.related_model if field_object._generated else prefetcher.model
        if (
            through_model._meta.default_connection != prefetcher.model._meta.default_connection
            or InstanceConnections.is_routed(through_model)
            or (through_model is not prefetcher.model and InstanceConnections.is_routed(prefetcher.model))
        ):
            return through_model.get_connection(for_write=False)
        return prefetcher.connection

    @staticmethod
    def get_parent_key_rows(
        prefetcher: Prefetcher, objs: Iterable[Model], connection: DatabaseClient
    ) -> list[tuple[Any, ...]]:
        """The objs' keys as the through table's key columns hold them, each once.

        Args:
            prefetcher: The prefetcher.
            objs: The objs whose relation is prefetched.

        Returns:
            The database values of each key.
        """
        parent_meta = prefetcher.model._meta
        if parent_meta.has_composite_primary_key:
            return list(set(KeyColumns.get_db_rows(parent_meta, objs, connection.dialect.types)))
        # The through table's key column is compared as a filter's __in list is - encoded at once.
        # The key's own column field - a one-to-one primary key's is its key column, not the relation.
        parent_key_name = parent_meta.primary_key_attribute_names[0]
        parent_key_values = list(dict.fromkeys(map(attrgetter(parent_key_name), objs)))
        encoded_values = ValueEncoders.encode_list(
            parent_key_values,
            prefetcher.model,
            parent_meta.fields_map[parent_key_name],
            connection.dialect,
        )
        return list({(value,) for value in encoded_values})

    @staticmethod
    async def read_through_rows(
        job: ManyToManyPrefetchJob, through_connection: DatabaseClient, *, reads_natively: bool
    ) -> Sequence[Any]:
        """The through table's rows of the objs - the owner's key columns, then the target's.

        Args:
            job: The prefetched relation.
            through_connection: The connection the through table is read on.
            reads_natively: Whether the rows are read by position.

        Returns:
            The rows.
        """
        prefetcher, field_object = job.prefetcher, job.field_object
        parent_pk_fields = prefetcher.model._meta.pk_fields
        through_table = Table(field_object.through, schema=field_object.through_schema)
        backward_columns = [through_table[column] for column in field_object.backward_keys]
        forward_columns = [through_table[column] for column in field_object.forward_keys]
        through_query = through_connection.query_class.from_(through_table).select(*backward_columns, *forward_columns)
        through_model = field_object.through_model_class
        if through_model is not None:
            # The raw through-table SELECT doesn't go through a manager - a through model's own
            # default scope is added here, under the parent query's visibility.
            from hare.query.scopes.row_scopes import RowScopes

            ambient_criterion = RowScopes.of(through_model).get_criterion(
                through_table,
                visibility=job.related_queryset._visibility,
                dialect=through_connection.dialect,
                connection=through_connection,
            )
            if ambient_criterion is not None:
                through_query = through_query.where(ambient_criterion)
        # One query per batch of owner keys - every key binds one parameter per column, and the
        # whole list can exceed the backend's own per-statement bind-parameter ceiling.
        parent_key_rows = ManyToManyPrefetch.get_parent_key_rows(prefetcher, job.objs, through_connection)
        batch_size = PrefetchKeys.get_batch_size(through_connection, len(backward_columns))
        through_batches: list[Sequence[Any]] = []
        for start in range(0, len(parent_key_rows), batch_size):
            # Through the dialect's __in operator - a long list binds as one parameter.
            batch_query = through_query.where(
                through_connection.dialect.filter_operators.get_row_membership_criterion(
                    backward_columns,
                    parent_key_rows[start : start + batch_size],
                    parent_pk_fields,
                    connection=through_connection,
                )
            )
            _, batch_rows = await through_connection.execute(
                *batch_query.get_parameterized_sql(), returns_rows=True, rows_by_position=reads_natively
            )
            through_batches.append(batch_rows)
        # One batch is read as the driver gave it - a native driver's rows are then read without a Python
        # object per row.
        if len(through_batches) == 1:
            return through_batches[0]
        return [row for batch in through_batches for row in batch]

    @staticmethod
    async def lay_out_many_to_many_rows(
        job: ManyToManyPrefetchJob, through_connection: DatabaseClient, through_rows: Sequence[Any]
    ) -> None:
        """The related rows fetched by their keys and collected as detached attachments -
        for a composite key on either side, or without ``rust.native.rows``.

        Args:
            job: The prefetched relation.
            through_connection: The connection the through rows were read on.
            through_rows: The through rows by column name.
        """
        related_objects_by_parent_key = await ManyToManyPrefetch.get_related_objects_by_parent_key(
            job, through_connection, through_rows
        )
        for obj in job.objs:
            parent_key = obj.pk if isinstance(obj.pk, tuple) else (obj.pk,)
            job.result.add_many(obj, job.field, job.to_attribute, related_objects_by_parent_key.get(parent_key, []))

    @staticmethod
    async def get_related_objects_by_parent_key(
        job: ManyToManyPrefetchJob, through_connection: DatabaseClient, through_rows: Sequence[Any]
    ) -> dict[tuple[Any, ...], list[Model]]:
        """The related rows of the through rows, fetched by their keys - each owner's in the related
        queryset's order.

        Args:
            job: The prefetched relation.
            through_connection: The connection the through rows were read on.
            through_rows: The through rows by column name.

        Returns:
            The related rows by owner key.
        """
        field_object, related_queryset = job.field_object, job.related_queryset
        target_meta = related_queryset.model._meta
        parent_pk_fields = job.prefetcher.model._meta.pk_fields
        target_pk_fields = target_meta.pk_fields
        parent_layout = PrefetchKeys.get_layout(parent_pk_fields, field_object.backward_keys, through_connection)
        target_layout = PrefetchKeys.get_layout(target_pk_fields, field_object.forward_keys, through_connection)
        parent_keys_by_target_key: defaultdict[tuple[Any, ...], list[tuple[Any, ...]]] = defaultdict(list)
        for row in through_rows:
            parent_key = parent_layout.read(row)
            target_key = target_layout.read(row)
            parent_keys_by_target_key[target_key].append(parent_key)

        related_objects_by_parent_key: dict[tuple[Any, ...], list[Model]] = {}
        if parent_keys_by_target_key:
            target_key_names = target_meta.primary_key_attribute_names
            related_queryset = PrefetchChecks.ensure_only_includes_fields(related_queryset, *target_key_names)
            # A single-column target key is filtered by its field name; a composite one by the
            # dialect's row-membership criterion, preserving the queryset's global ordering.
            if len(target_key_names) == 1:
                related_objects = await related_queryset.filter(
                    **{f"{target_key_names[0]}__in": [target_key[0] for target_key in parent_keys_by_target_key]}
                )
            else:
                related_objects = await related_queryset.filter(pk__in=list(parent_keys_by_target_key))
            for related_object in related_objects:
                related_key = related_object.pk if isinstance(related_object.pk, tuple) else (related_object.pk,)
                for parent_key in parent_keys_by_target_key.get(related_key, []):
                    group = related_objects_by_parent_key.get(parent_key)
                    if group is None:
                        group = related_objects_by_parent_key[parent_key] = []
                    group.append(related_object)
        return related_objects_by_parent_key

    @staticmethod
    async def prefetch_sliced_many_to_many_relation(job: ManyToManyPrefetchJob) -> None:
        """``ManyToManyPrefetch.prefetch_many_to_many_relation()`` of a sliced queryset: the slice of each
        owner's related rows.
        A related row may belong to several owners and fall into one owner's slice only, so the
        (owner, related row) pairs of the slices are read first - numbered over the relation back
        to the owners - and then the related rows by their keys.

        Args:
            job: The prefetched relation - its related queryset sliced.
        """
        objs, related_queryset = job.objs, job.related_queryset
        parent_key_names = job.prefetcher.model._meta.primary_key_attribute_names
        target_key_names = related_queryset.model._meta.primary_key_attribute_names
        related_name = job.field_object.related_name
        parent_paths = tuple(f"{related_name}__{name}" for name in parent_key_names)
        rows_by_parent: defaultdict[tuple[Any, ...], list[Model]] = defaultdict(list)
        objs = list(objs)
        # The owners' composite keys are OR-ed - a database may bound the depth of an expression.
        bounds_depth = related_queryset.get_connection().features.max_expression_depth is not None
        parent_batch_size = (
            PREFETCH_MAX_SLICED_COMPOSITE_PARENTS if bounds_depth and len(parent_key_names) > 1 else len(objs)
        )
        pairs: list[Any] = []
        for start in range(0, len(objs), max(1, parent_batch_size)):
            parent_condition = RelatedRowsFetch.get_parent_keys_condition(
                objs[start : start + parent_batch_size], parent_key_names, parent_paths
            )
            if parent_condition is not None:
                pairs.extend(
                    await SlicedPrefetch.get_numbered_rows(related_queryset, parent_paths, parent_condition)
                    .order_by(*parent_paths, PREFETCH_ROW_NUMBER_ANNOTATION)
                    .values_list(*parent_paths, *target_key_names)
                )
        if pairs:
            parent_width = len(parent_paths)
            target_keys = list(dict.fromkeys(tuple(pair[parent_width:]) for pair in pairs))
            related_by_key: dict[tuple[Any, ...], Model] = {}
            if target_keys:
                unsliced = PrefetchChecks.ensure_only_includes_fields(
                    SlicedPrefetch.get_unsliced(related_queryset), *target_key_names
                )
                if len(target_key_names) == 1:
                    related_objects = await unsliced.filter(
                        **{f"{target_key_names[0]}__in": [key[0] for key in target_keys]}
                    )
                else:
                    groups = [Q(**dict(zip(target_key_names, key, strict=True))) for key in target_keys]
                    related_objects = await RelatedRowsFetch.fetch_matching_any_group(
                        unsliced, groups, key_width=len(target_key_names)
                    )
                related_by_key = {
                    PrefetchKeys.get_value(related_object, target_key_names): related_object
                    for related_object in related_objects
                }
            for pair in pairs:
                related_object = related_by_key.get(tuple(pair[parent_width:]))
                if related_object is not None:
                    rows_by_parent[tuple(pair[:parent_width])].append(related_object)
        for obj in objs:
            parent_key = PrefetchKeys.get_value(obj, parent_key_names)
            job.result.add_many(obj, job.field, job.to_attribute, rows_by_parent.get(parent_key, []))

    @staticmethod
    def can_join_through_table(
        related_queryset: QuerySet[Any], field_object: ManyToManyFieldInstance[Any], connection: DatabaseClient
    ) -> bool:
        """Whether the related rows of a many-to-many prefetch can be read in one query, joined to
        the through table on the related model's own side of the relation: both are read on the
        parents' connection, the through table has no model of its own (whose default scope the
        raw read adds), and the related queryset neither filters by that relation itself, nor
        annotates, deduplicates or loads further relations, explicitly or by default - each related
        row then comes once per owner.

        Args:
            related_queryset: The target queryset with its selected connection.
            field_object: The bound many-to-many relation.
            connection: The parent connection the through table would be joined on.

        Returns:
            True when it can.
        """
        target_model = related_queryset.model
        related_name = field_object.related_name
        if (
            field_object.through_model_class is not None
            or not isinstance(target_model._meta.fields_map.get(related_name), ManyToManyFieldInstance)
            # The related rows are read where the through table is.
            or related_queryset.get_connection() is not connection
            or related_queryset._q_object_list
            or related_queryset._annotations
            or related_queryset._loads_relations()
            or related_queryset._extension_calls
            or related_queryset._distinct
            or related_queryset._combination is not None
        ):
            return False
        relation_path_prefix = f"{related_name}__"
        for _negate, _generation, conditions, kwargs in related_queryset._pending_filter_calls:
            # A Q condition filters like one built - the related queryset then filters by conditions.
            if conditions:
                return False
            if any(key == related_name or key.startswith(relation_path_prefix) for key in kwargs):
                return False
        return True

    @staticmethod
    async def prefetch_many_to_many_by_join(job: ManyToManyPrefetchJob, parent_key_values: list[Any]) -> None:
        """``ManyToManyPrefetch.prefetch_many_to_many_relation()`` in one query: the related rows filtered by
        the relation back
        to the owners - joined to the through table - each with its owner's key, through
        ``rust.native.rows``. A related row of several owners comes once per owner and is one
        instance in each owner's rows.

        Args:
            job: The prefetched relation.
            parent_key_values: The objs' keys, each once.
        """
        native_rows = NativeModules.rows
        related_queryset, field_object = job.related_queryset, job.field_object
        # Each key by its own column - a one-to-one primary key's is its key column.
        parent_key_name = job.prefetcher.model._meta.primary_key_attribute_names[0]
        related_name = field_object.related_name
        related_queryset = PrefetchChecks.ensure_only_includes_fields(
            related_queryset, *related_queryset.model._meta.primary_key_attribute_names
        )
        rows_query = related_queryset.filter(**{f"{related_name}__in": parent_key_values})._annotate(
            {PREFETCH_PARENT_KEY_ANNOTATION: F(f"{related_name}__{parent_key_name}")}
        )
        related_rows = await QueryExecution.run_operation(
            (rows_query,),
            partial(StatementPlanRuns.read_queryset, rows_query, model_query_class=GroupedModelRowsQuery),
        )
        related_objects_by_parent = native_rows.group_by_item(related_rows, 0, 1)
        return ManyToManyPrefetch.set_related_objects_by_parent(job, parent_key_name, related_objects_by_parent)

    @staticmethod
    async def lay_out_many_to_many_rows_natively(
        job: ManyToManyPrefetchJob, through_connection: DatabaseClient, through_rows: Sequence[Any]
    ) -> None:
        """The rest of ``ManyToManyPrefetch.prefetch_many_to_many_relation()`` for single-column keys on both
        sides, through
        ``rust.native.rows``: the through rows' keys read by the field codecs, the related rows
        fetched by their keys and collected as detached attachments - a call each.

        Args:
            job: The prefetched relation.
            through_connection: The connection the through rows were read on.
            through_rows: The through rows - the owner's key column, then the target's.
        """
        native_rows = NativeModules.rows
        prefetcher, related_queryset = job.prefetcher, job.related_queryset
        # Each key by its own column field - a one-to-one primary key's is its key column.
        parent_meta = prefetcher.model._meta
        parent_key_name = parent_meta.primary_key_attribute_names[0]
        parent_key_field = parent_meta.fields_map[parent_key_name]
        target_meta = related_queryset.model._meta
        target_key_name = target_meta.primary_key_attribute_names[0]
        target_key_field = target_meta.fields_map[target_key_name]
        parents_by_target_key: dict[Any, list[Any]] = {}
        if through_rows:
            layout = PrefetchKeys.get_layout((parent_key_field, target_key_field), (0, 1), through_connection)
            keys = layout.read_many(through_rows)
            parents_by_target_key = native_rows.group_by_item(keys, 1, 0)
        related_objects: list[Model] = []
        if parents_by_target_key:
            related_queryset = PrefetchChecks.ensure_only_includes_fields(related_queryset, target_key_name)
            related_objects = await related_queryset.filter(**{f"{target_key_name}__in": list(parents_by_target_key)})
        related_objects_by_parent = native_rows.group_related(related_objects, target_key_name, parents_by_target_key)
        return ManyToManyPrefetch.set_related_objects_by_parent(job, parent_key_name, related_objects_by_parent)

    @staticmethod
    def set_related_objects_by_parent(
        job: ManyToManyPrefetchJob, parent_key_name: str, related_objects_by_parent: dict[Any, list[Model]]
    ) -> None:
        """Collect each obj's detached related rows, read from ``related_objects_by_parent`` by its key.

        Args:
            job: The prefetched relation.
            parent_key_name: The objs' key attribute.
            related_objects_by_parent: The related rows by their owner's key.
        """
        for obj in job.objs:
            job.result.add_many(
                obj,
                job.field,
                job.to_attribute,
                related_objects_by_parent.get(getattr(obj, parent_key_name), []),
            )
