from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.constants import PREFETCH_MAX_SLICED_COMPOSITE_PARENTS
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.relation_accessors import RelationAccessors
from hare.query.expressions import Subquery
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks
from hare.query.relation_loading.prefetching.related_rows_fetch import RelatedRowsFetch
from hare.query.relation_loading.prefetching.sliced_prefetch import SlicedPrefetch
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.queryset import QuerySet
    from hare.query.relation_loading.prefetching.prefetcher import Prefetcher


class ReverseRelationPrefetch:
    """The prefetch of a reverse foreign key or one-to-one relation: the related rows fetched by the
    parents' keys and laid out on each parent."""

    @staticmethod
    async def prefetch_reverse_relation(
        prefetcher: Prefetcher,
        objs: Iterable[Model],
        field: str,
        related_query: tuple[str | None, QuerySet[Any]],
    ) -> Iterable[Model]:
        """Loads a backward foreign key relation onto the objects by one query of the related rows.

        Args:
            prefetcher: The prefetcher.
            objs: The objects.
            field: The relation's name.
            related_query: The attribute the rows go to (the relation when None) and their queryset.

        Returns:
            The objects.

        Raises:
            QueryError: An object is unsaved, or didn't load the key the relation reads.
        """
        to_attribute, related_query = related_query
        PrefetchChecks.reject_unsaved_instances(objs)
        related_field: BackwardForeignKeyRelation[Any] = prefetcher.model._meta.fields_map[field]  # type: ignore[assignment]
        related_field_names = tuple(to_field.model_field_name for to_field in related_field.to_field_instances)
        PrefetchChecks.reject_unloaded_relation_keys(objs, related_field_names, field)
        relation_fields = related_field.relation_fields
        sliced = SlicedPrefetch.is_sliced(related_query)
        if (
            sliced
            and len(relation_fields) > 1
            and related_query.model._meta.connection.features.max_expression_depth is not None
        ):
            objs = list(objs)
            if len(objs) > PREFETCH_MAX_SLICED_COMPOSITE_PARENTS:
                # The parents' composite keys are OR-ed - this database bounds the depth of an expression.
                for start in range(0, len(objs), PREFETCH_MAX_SLICED_COMPOSITE_PARENTS):
                    await ReverseRelationPrefetch.prefetch_reverse_relation(
                        prefetcher,
                        objs[start : start + PREFETCH_MAX_SLICED_COMPOSITE_PARENTS],
                        field,
                        (to_attribute, related_query),
                    )
                return objs
        # The slice of each parent's rows, when they are read whole.
        owner_slice: slice | None = None
        if sliced and (
            not related_query.model._meta.has_primary_key or SlicedPrefetch.repeats_rows_of_a_key(related_query)
        ):
            # Rows of no key, or rows a to-many filter repeats, can't be picked by their keys - each parent's
            # rows are read and sliced here, as the queryset's own slice would take them.
            offset = related_query._offset or 0
            owner_slice = slice(offset, None if related_query._limit is None else offset + related_query._limit)
            related_query = SlicedPrefetch.get_unsliced(related_query)
        elif sliced:
            # The rows of each parent's slice, picked by their keys in one subquery.
            parent_condition = RelatedRowsFetch.get_parent_keys_condition(objs, related_field_names, relation_fields)
            unsliced = SlicedPrefetch.get_unsliced(related_query)
            if parent_condition is not None:
                key_names = related_query.model._meta.primary_key_attribute_names
                numbered_keys = SlicedPrefetch.get_numbered_rows(
                    related_query, relation_fields, parent_condition
                ).values(*key_names)
                key_path = "pk" if len(key_names) > 1 else key_names[0]
                unsliced = unsliced.filter(**{f"{key_path}__in": Subquery(numbered_keys)})
            related_query = unsliced

        if len(relation_fields) == 1:
            related_field_name = related_field_names[0]
            relation_field = relation_fields[0]
            related_objects_for_fetch = RelatedRowsFetch.collect_related_fetch_values(
                objs, related_field_name, relation_field
            )
            related_query = PrefetchChecks.ensure_only_includes_fields(related_query, relation_field)
            related_object_list = await related_query.filter(
                **{f"{field_name}__in": key_values for field_name, key_values in related_objects_for_fetch.items()}
            )

            native_rows = HydrateAccelerator.module
            if native_rows is not None and not to_attribute and owner_slice is None:
                # The rows grouped by their key and handed to each instance's relation in one call
                # each - the loops below.
                native_rows.set_prefetched_rows(
                    objs,
                    related_field_name,
                    native_rows.group_by_attribute(related_object_list, relation_field),
                    f"_{field}",
                    RelationRows.make_for_prefetch,
                )
                return objs
            related_object_map: dict[Any, list[Model]] = {}
            for entry in related_object_list:
                object_id = getattr(entry, relation_field)
                related_object_map.setdefault(object_id, []).append(entry)
            for instance in objs:
                relation_container = RelationAccessors.get_relation(instance, field)
                instance_rows = related_object_map.get(getattr(instance, related_field_name), [])
                relation_container._set_result_for_query(
                    instance_rows if owner_slice is None else instance_rows[owner_slice], to_attribute
                )
            return objs

        related_object_map = {}
        for entry in await RelatedRowsFetch.fetch_by_composite_keys(
            objs, related_query, related_field_names, relation_fields
        ):
            object_id = tuple(getattr(entry, name) for name in relation_fields)
            related_object_map.setdefault(object_id, []).append(entry)

        for instance in objs:
            relation_container = RelationAccessors.get_relation(instance, field)
            row = tuple(getattr(instance, name) for name in related_field_names)
            instance_rows = related_object_map.get(row, [])
            relation_container._set_result_for_query(
                instance_rows if owner_slice is None else instance_rows[owner_slice], to_attribute
            )
        return objs

    @staticmethod
    async def prefetch_reverse_one_to_one_relation(
        prefetcher: Prefetcher,
        objs: Iterable[Model],
        field: str,
        related_query: tuple[str | None, QuerySet[Any]],
    ) -> Iterable[Model]:
        """Loads a backward one-to-one relation onto the objects by one query of the related rows.

        Args:
            prefetcher: The prefetcher.
            objs: The objects.
            field: The relation's name.
            related_query: The attribute the row goes to (the relation when None) and its queryset.

        Returns:
            The objects.
        """
        to_attribute, related_queryset = related_query
        if SlicedPrefetch.is_sliced(related_queryset):
            if not SlicedPrefetch.keeps_the_only_row(related_queryset):
                # A parent has one row at most - a slice past it leaves none.
                for instance in objs:
                    setattr(instance, to_attribute or f"_{field}", None)
                return objs
            related_queryset = SlicedPrefetch.get_unsliced(related_queryset)
        PrefetchChecks.reject_unsaved_instances(objs)
        related_field = cast("BackwardOneToOneRelation[Any]", prefetcher.model._meta.fields_map[field])
        related_field_names = tuple(to_field.model_field_name for to_field in related_field.to_field_instances)
        PrefetchChecks.reject_unloaded_relation_keys(objs, related_field_names, field)
        relation_fields = related_field.relation_fields

        related_object_map: dict[Any, Model] = {}
        if len(relation_fields) == 1:
            related_field_name = related_field_names[0]
            relation_field = relation_fields[0]
            related_objects_for_fetch = RelatedRowsFetch.collect_related_fetch_values(
                objs, related_field_name, relation_field
            )
            related_queryset = PrefetchChecks.ensure_only_includes_fields(related_queryset, relation_field)
            related_object_list = await related_queryset.filter(
                **{f"{field_name}__in": key_values for field_name, key_values in related_objects_for_fetch.items()}
            )
            for entry in related_object_list:
                related_object_map[getattr(entry, relation_field)] = entry
            for instance in objs:
                obj = related_object_map.get(getattr(instance, related_field_name))
                # A to_attribute prefetch leaves the bare relation unfetched - several Prefetch(field,
                # to_attribute=...) of one relation don't overwrite each other.
                if to_attribute:
                    setattr(instance, to_attribute, obj)
                else:
                    setattr(instance, f"_{field}", obj)
            return objs

        for entry in await RelatedRowsFetch.fetch_by_composite_keys(
            objs, related_queryset, related_field_names, relation_fields
        ):
            related_object_map[tuple(getattr(entry, name) for name in relation_fields)] = entry

        for instance in objs:
            row = tuple(getattr(instance, name) for name in related_field_names)
            obj = related_object_map.get(row)
            # See the single-column branch above for why to_attribute and the bare cache are mutually
            # exclusive here.
            if to_attribute:
                setattr(instance, to_attribute, obj)
            else:
                setattr(instance, f"_{field}", obj)
        return objs
