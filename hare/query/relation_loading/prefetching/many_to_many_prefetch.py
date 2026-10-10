from __future__ import annotations

from collections.abc import Iterable, Sequence
from operator import attrgetter
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.constants import PREFETCH_BIND_PARAMETERS_HEADROOM, PREFETCH_MAX_COMPOSITE_ROWS
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.relation_accessors import RelationAccessors
from hare.models.instances.instance_connections import InstanceConnections
from hare.query.expressions import F, Q
from hare.query.filters.lookups.value_encoders import ValueEncoders
from hare.query.key_columns import KeyColumns
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows
from hare.query.relation_loading.constants import PREFETCH_OWNER_KEY_ANNOTATION, PREFETCH_ROW_NUMBER_ANNOTATION
from hare.query.relation_loading.prefetching.declarations import ManyToManyPrefetchJob
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks
from hare.query.relation_loading.prefetching.related_rows_fetch import RelatedRowsFetch
from hare.query.relation_loading.prefetching.sliced_prefetch import SlicedPrefetch
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from hare.query.rows.values_rows.value_field import ValueField
from hare.sql.builder.tables.table import Table

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.field import Field
    from hare.models import Model
    from hare.models.meta_info import MetaInfo
    from hare.query.queryset import QuerySet
    from hare.query.relation_loading.prefetching.prefetcher import Prefetcher


class ManyToManyPrefetch:
    """The prefetch of a many-to-many relation: the related rows read through the through table -
    joined to it in one query where possible, else in two - sliced per parent when asked, and laid
    out on each parent."""

    @staticmethod
    async def prefetch_many_to_many_relation(
        prefetcher: Prefetcher,
        objs: Iterable[Model],
        field: str,
        related_query: tuple[str | None, QuerySet[Any]],
    ) -> Iterable[Model]:
        """Fetches the related rows of a many-to-many relation and sets them on the objs.

        Args:
            prefetcher: The prefetcher.
            objs: The objs whose relation is prefetched.
            field: The many-to-many field's name.
            related_query: The attribute the related rows are set as (None for the relation itself)
                and the related rows' queryset.

        Returns:
            The objs.
        """
        to_attribute, related_queryset = related_query
        PrefetchChecks.reject_unsaved_instances(objs)

        field_object: ManyToManyFieldInstance[Any] = prefetcher.model._meta.fields_map[field]  # type: ignore[assignment]
        PrefetchChecks.reject_many_to_many_prefetch_queryset_model_mismatch(related_queryset, field, field_object)
        job = ManyToManyPrefetchJob(prefetcher, objs, field, field_object, to_attribute, related_queryset)
        if SlicedPrefetch.is_sliced(related_queryset):
            return await ManyToManyPrefetch.prefetch_sliced_many_to_many_relation(job)
        owner_meta = prefetcher.model._meta
        target_meta = related_queryset.model._meta
        through_connection = ManyToManyPrefetch.get_through_connection(prefetcher, field_object)
        if (
            through_connection is prefetcher.connection
            and HydrateAccelerator.module is not None
            and prefetcher.connection.features.supports_positional_rows
            and not owner_meta.has_composite_primary_key
            and not target_meta.has_composite_primary_key
            and ManyToManyPrefetch.can_join_through_table(job)
        ):
            owner_key_values = list(dict.fromkeys(map(attrgetter(owner_meta.primary_key_attribute_names[0]), objs)))
            # More keys than one query binds keep the through-table read, batched.
            if (
                len(owner_key_values)
                <= prefetcher.connection.features.max_bind_parameters - PREFETCH_BIND_PARAMETERS_HEADROOM
            ):
                return await ManyToManyPrefetch.prefetch_many_to_many_by_join(job, owner_key_values)
        # The through table is read first, then the related rows by primary key - the same for a
        # single-column and a composite key on either side.
        # The through rows are read by position where rust.native.rows lays them out, by column name
        # otherwise.
        reads_natively = (
            HydrateAccelerator.module is not None
            and through_connection.features.supports_positional_rows
            and len(ManyToManyPrefetch.get_key_fields(owner_meta)) == 1
            and len(ManyToManyPrefetch.get_key_fields(target_meta)) == 1
        )
        through_rows = await ManyToManyPrefetch.read_through_rows(
            job, through_connection, reads_natively=reads_natively
        )
        if reads_natively:
            return await ManyToManyPrefetch.lay_out_many_to_many_rows_natively(job, through_connection, through_rows)
        return await ManyToManyPrefetch.lay_out_many_to_many_rows(job, through_connection, through_rows)

    @staticmethod
    def get_key_fields(meta: MetaInfo) -> tuple[Field[Any], ...]:
        """The fields of a model's primary key.

        Args:
            meta: The model's meta.

        Returns:
            The key fields - one for a single-column key.
        """
        return meta.pk_fields if meta.has_composite_primary_key else (meta.pk,)

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
        owning_model = field_object.related_model if field_object._generated else prefetcher.model
        if (
            owning_model._meta.default_connection != prefetcher.model._meta.default_connection
            or InstanceConnections.is_routed(owning_model)
            or (owning_model is not prefetcher.model and InstanceConnections.is_routed(prefetcher.model))
        ):
            return owning_model.get_connection(for_write=False)
        return prefetcher.connection

    @staticmethod
    def get_owner_key_rows(prefetcher: Prefetcher, objs: Iterable[Model]) -> list[tuple[Any, ...]]:
        """The objs' keys as the through table's key columns hold them, each once.

        Args:
            prefetcher: The prefetcher.
            objs: The objs whose relation is prefetched.

        Returns:
            The database values of each key.
        """
        owner_meta = prefetcher.model._meta
        if owner_meta.has_composite_primary_key:
            return list(
                {KeyColumns.get_db_values(owner_meta, obj, prefetcher.connection.dialect.types) for obj in objs}
            )
        # The through table's key column is compared as a filter's __in list is - encoded at once.
        # The key's own column field - a one-to-one primary key's is its key column, not the relation.
        owner_key_name = owner_meta.primary_key_attribute_names[0]
        owner_key_values = list(dict.fromkeys(map(attrgetter(owner_key_name), objs)))
        encoded_values = ValueEncoders.encode_list(
            owner_key_values,
            cast("Model", prefetcher.model),
            owner_meta.fields_map[owner_key_name],
            prefetcher.connection.dialect,
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
        owner_pk_fields = ManyToManyPrefetch.get_key_fields(prefetcher.model._meta)
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
                dialect=prefetcher.connection.dialect,
                connection=prefetcher.connection,
            )
            if ambient_criterion is not None:
                through_query = through_query.where(ambient_criterion)
        # One query per batch of owner keys - every key binds one parameter per column, and the
        # whole list can exceed the backend's own per-statement bind-parameter ceiling.
        owner_key_rows = ManyToManyPrefetch.get_owner_key_rows(prefetcher, job.objs)
        batch_size = max(
            1,
            (through_connection.features.max_bind_parameters - PREFETCH_BIND_PARAMETERS_HEADROOM)
            // len(backward_columns),
        )
        if len(backward_columns) > 1:
            batch_size = min(batch_size, PREFETCH_MAX_COMPOSITE_ROWS)
        through_batches: list[Sequence[Any]] = []
        for start in range(0, len(owner_key_rows), batch_size):
            # Through the dialect's __in operator - a long list binds as one parameter.
            batch_query = through_query.where(
                through_connection.dialect.filter_operators.get_row_membership_criterion(
                    backward_columns, owner_key_rows[start : start + batch_size], owner_pk_fields
                )
            )
            _, batch_rows = await through_connection.execute(
                *batch_query.get_parameterized_sql(), returns_rows=True, rows_by_position=reads_natively
            )
            through_batches.append(batch_rows)
        # One batch is read as the driver gave it - rust_pg's rows are then read without a Python
        # object per row.
        if len(through_batches) == 1:
            return through_batches[0]
        return [row for batch in through_batches for row in batch]

    @staticmethod
    async def lay_out_many_to_many_rows(
        job: ManyToManyPrefetchJob, through_connection: DatabaseClient, through_rows: Sequence[Any]
    ) -> Iterable[Model]:
        """The related rows of the through rows fetched by their keys and set on the objs' relations -
        for a composite key on either side, or without ``rust.native.rows``.

        Args:
            job: The prefetched relation.
            through_connection: The connection the through rows were read on.
            through_rows: The through rows by column name.

        Returns:
            The objs.
        """
        field_object, related_queryset = job.field_object, job.related_queryset
        target_meta = related_queryset.model._meta
        owner_pk_fields = ManyToManyPrefetch.get_key_fields(job.prefetcher.model._meta)
        target_pk_fields = ManyToManyPrefetch.get_key_fields(target_meta)
        get_python_value = through_connection.dialect.types.get_python_value
        owner_keys_by_target_key: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
        for row in through_rows:
            owner_key = tuple(
                get_python_value(key_field, row[column])
                for key_field, column in zip(owner_pk_fields, field_object.backward_keys, strict=True)
            )
            target_key = tuple(
                get_python_value(key_field, row[column])
                for key_field, column in zip(target_pk_fields, field_object.forward_keys, strict=True)
            )
            owner_keys_by_target_key.setdefault(target_key, []).append(owner_key)

        related_objects_by_owner_key: dict[tuple[Any, ...], list[Model]] = {}
        if owner_keys_by_target_key:
            target_key_names = target_meta.primary_key_attribute_names
            related_queryset = PrefetchChecks.ensure_only_includes_fields(related_queryset, *target_key_names)
            # A single-column target key is filtered by its field name; a composite one by an
            # AND-group per key, OR-ed in batches.
            if len(target_key_names) == 1:
                related_objects = await related_queryset.filter(
                    **{f"{target_key_names[0]}__in": [target_key[0] for target_key in owner_keys_by_target_key]}
                )
            else:
                groups = [
                    Q(**dict(zip(target_key_names, target_key, strict=True)))
                    for target_key in owner_keys_by_target_key
                ]
                related_objects = await RelatedRowsFetch.fetch_matching_any_group(related_queryset, groups)
            for related_object in related_objects:
                related_key = related_object.pk if isinstance(related_object.pk, tuple) else (related_object.pk,)
                for owner_key in owner_keys_by_target_key.get(related_key, []):
                    related_objects_by_owner_key.setdefault(owner_key, []).append(related_object)

        for obj in job.objs:
            owner_key = obj.pk if isinstance(obj.pk, tuple) else (obj.pk,)
            RelationAccessors.get_relation(obj, job.field)._set_result_for_query(
                related_objects_by_owner_key.get(owner_key, []), job.to_attribute
            )
        return job.objs

    @staticmethod
    async def prefetch_sliced_many_to_many_relation(job: ManyToManyPrefetchJob) -> Iterable[Model]:
        """``ManyToManyPrefetch.prefetch_many_to_many_relation()`` of a sliced queryset: the slice of each
        owner's related rows.
        A related row may belong to several owners and fall into one owner's slice only, so the
        (owner, related row) pairs of the slices are read first - numbered over the relation back
        to the owners - and then the related rows by their keys.

        Args:
            job: The prefetched relation - its related queryset sliced.

        Returns:
            The objs.
        """
        objs, related_queryset = job.objs, job.related_queryset
        owner_key_names = job.prefetcher.model._meta.primary_key_attribute_names
        target_key_names = related_queryset.model._meta.primary_key_attribute_names
        related_name = job.field_object.related_name
        owner_paths = tuple(f"{related_name}__{name}" for name in owner_key_names)
        rows_by_owner: dict[tuple[Any, ...], list[Model]] = {}
        parent_condition = RelatedRowsFetch.get_parent_keys_condition(objs, owner_key_names, owner_paths)
        if parent_condition is not None:
            pairs = await (
                SlicedPrefetch.get_numbered_rows(related_queryset, owner_paths, parent_condition)
                .order_by(*owner_paths, PREFETCH_ROW_NUMBER_ANNOTATION)
                .values_list(*owner_paths, *target_key_names)
            )
            owner_width = len(owner_paths)
            target_keys = list(dict.fromkeys(tuple(pair[owner_width:]) for pair in pairs))
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
                    related_objects = await RelatedRowsFetch.fetch_matching_any_group(unsliced, groups)
                related_by_key = {
                    tuple(getattr(related_object, name) for name in target_key_names): related_object
                    for related_object in related_objects
                }
            for pair in pairs:
                related_object = related_by_key.get(tuple(pair[owner_width:]))
                if related_object is not None:
                    rows_by_owner.setdefault(tuple(pair[:owner_width]), []).append(related_object)
        for obj in objs:
            owner_key = tuple(getattr(obj, name) for name in owner_key_names)
            RelationAccessors.get_relation(obj, job.field)._set_result_for_query(
                rows_by_owner.get(owner_key, []), job.to_attribute
            )
        return objs

    @staticmethod
    def can_join_through_table(job: ManyToManyPrefetchJob) -> bool:
        """Whether the related rows of a many-to-many prefetch can be read in one query, joined to
        the through table on the related model's own side of the relation: both are read on the
        parents' connection, the through table has no model of its own (whose default scope the
        raw read adds), and the related queryset neither filters by that relation itself, nor
        annotates, deduplicates or prefetches further - each related row then comes once per owner.

        Args:
            job: The prefetched relation.

        Returns:
            True when it can.
        """
        related_queryset, field_object = job.related_queryset, job.field_object
        target_model = related_queryset.model
        related_name = field_object.related_name
        if (
            field_object.through_model_class is not None
            or not isinstance(target_model._meta.fields_map.get(related_name), ManyToManyFieldInstance)
            # The related rows are read where the through table is.
            or related_queryset.get_connection() is not job.prefetcher.connection
            or related_queryset._q_object_list
            or related_queryset._annotations
            or related_queryset._distinct
            or related_queryset._combination is not None
            or related_queryset._prefetch_map
            or related_queryset._prefetch_queries
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
    async def prefetch_many_to_many_by_join(
        job: ManyToManyPrefetchJob, owner_key_values: list[Any]
    ) -> Iterable[Model]:
        """``ManyToManyPrefetch.prefetch_many_to_many_relation()`` in one query: the related rows filtered by
        the relation back
        to the owners - joined to the through table - each with its owner's key, through
        ``rust.native.rows``. A related row of several owners comes once per owner and is one
        instance in each owner's rows.

        Args:
            job: The prefetched relation.
            owner_key_values: The objs' keys, each once.

        Returns:
            The objs.
        """
        native_rows = HydrateAccelerator.module
        related_queryset, field_object = job.related_queryset, job.field_object
        # Each key by its own column - a one-to-one primary key's is its key column.
        owner_key_name = job.prefetcher.model._meta.primary_key_attribute_names[0]
        target_key_name = related_queryset.model._meta.primary_key_attribute_names[0]
        related_name = field_object.related_name
        related_queryset = PrefetchChecks.ensure_only_includes_fields(related_queryset, target_key_name)
        related_rows = await related_queryset.filter(**{f"{related_name}__in": owner_key_values}).annotate(
            **{PREFETCH_OWNER_KEY_ANNOTATION: F(f"{related_name}__{owner_key_name}")}
        )
        related_objects_by_owner = native_rows.group_rows_by_owner(
            related_rows, target_key_name, PREFETCH_OWNER_KEY_ANNOTATION
        )
        return ManyToManyPrefetch.set_related_objects_by_owner(job, owner_key_name, related_objects_by_owner)

    @staticmethod
    async def lay_out_many_to_many_rows_natively(
        job: ManyToManyPrefetchJob, through_connection: DatabaseClient, through_rows: Sequence[Any]
    ) -> Iterable[Model]:
        """The rest of ``ManyToManyPrefetch.prefetch_many_to_many_relation()`` for single-column keys on both
        sides, through
        ``rust.native.rows``: the through rows' keys read by the field codecs, the related rows
        fetched by their keys and laid out on the objs' relations - a call each.

        Args:
            job: The prefetched relation.
            through_connection: The connection the through rows were read on.
            through_rows: The through rows - the owner's key column, then the target's.

        Returns:
            The objs.
        """
        native_rows = HydrateAccelerator.module
        prefetcher, related_queryset = job.prefetcher, job.related_queryset
        # Each key by its own column field - a one-to-one primary key's is its key column.
        owner_meta = prefetcher.model._meta
        owner_key_name = owner_meta.primary_key_attribute_names[0]
        owner_key_field = owner_meta.fields_map[owner_key_name]
        target_meta = related_queryset.model._meta
        target_key_name = target_meta.primary_key_attribute_names[0]
        target_key_field = target_meta.fields_map[target_key_name]
        owners_by_target_key: dict[Any, list[Any]] = {}
        if through_rows:
            key_reader = HydrateAccelerator.get_values_reader(
                prefetcher.model,
                (
                    ValueField(prefetcher.model, owner_key_name, owner_key_field, False),
                    ValueField(related_queryset.model, target_key_name, target_key_field, False),
                ),
                through_connection,
            )
            owners_by_target_key = native_rows.group_by_item(key_reader.read_tuples(through_rows), 1, 0)
        related_objects: list[Model] = []
        if owners_by_target_key:
            related_queryset = PrefetchChecks.ensure_only_includes_fields(related_queryset, target_key_name)
            related_objects = await related_queryset.filter(**{f"{target_key_name}__in": list(owners_by_target_key)})
        related_objects_by_owner = native_rows.group_related(related_objects, target_key_name, owners_by_target_key)
        return ManyToManyPrefetch.set_related_objects_by_owner(job, owner_key_name, related_objects_by_owner)

    @staticmethod
    def set_related_objects_by_owner(
        job: ManyToManyPrefetchJob, owner_key_name: str, related_objects_by_owner: dict[Any, list[Model]]
    ) -> Iterable[Model]:
        """Sets each obj's related rows - read off ``related_objects_by_owner`` by the obj's key.

        Args:
            job: The prefetched relation.
            owner_key_name: The objs' key attribute.
            related_objects_by_owner: The related rows by their owner's key.

        Returns:
            The objs.
        """
        if job.to_attribute:
            for obj in job.objs:
                RelationAccessors.get_relation(obj, job.field)._set_result_for_query(
                    related_objects_by_owner.get(getattr(obj, owner_key_name), []), job.to_attribute
                )
            return job.objs
        HydrateAccelerator.module.set_prefetched_rows(
            job.objs,
            owner_key_name,
            related_objects_by_owner,
            f"_{job.field}",
            RelationRows.make_for_prefetch,
        )
        return job.objs
