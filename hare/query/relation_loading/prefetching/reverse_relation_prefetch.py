from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.relation_accessors import RelationAccessors
from hare.query.expressions import Subquery
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks
from hare.query.relation_loading.prefetching.related_rows_fetch import RelatedRowsFetch
from hare.query.relation_loading.prefetching.sliced_prefetch import SlicedPrefetch
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator

if TYPE_CHECKING:  # pragma: nocoverage
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
        to_attribute, related_query = related_query
        PrefetchChecks.reject_unsaved_instances(objs)
        related_field: BackwardForeignKeyRelation[Any] = prefetcher.model._meta.fields_map[field]  # type: ignore[assignment]
        related_field_names = tuple(to_field.model_field_name for to_field in related_field.to_field_instances)
        PrefetchChecks.reject_unloaded_relation_keys(objs, related_field_names, field)
        relation_fields = related_field.relation_fields
        if SlicedPrefetch.is_sliced(related_query):
            # The rows of each parent's slice, picked by their keys in one subquery.
            parent_condition = RelatedRowsFetch.get_parent_keys_condition(objs, related_field_names, relation_fields)
            unsliced = SlicedPrefetch.get_unsliced(related_query)
            if parent_condition is not None:
                key_names = related_query.model._meta.primary_key_attribute_names
                numbered_keys = SlicedPrefetch.get_numbered_rows(
                    related_query, relation_fields, parent_condition
                ).values(*key_names)
                key_path = "pk" if len(key_names) > 1 else key_names[0]
                # Subquery() takes a queryset as it takes the query a queryset builds.
                unsliced = unsliced.filter(**{f"{key_path}__in": Subquery(numbered_keys)})  # type: ignore[arg-type]
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
            if native_rows is not None and not to_attribute:
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
                relation_container._set_result_for_query(
                    related_object_map.get(getattr(instance, related_field_name), []),
                    to_attribute,
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
            relation_container._set_result_for_query(related_object_map.get(row, []), to_attribute)
        return objs

    @staticmethod
    async def prefetch_reverse_one_to_one_relation(
        prefetcher: Prefetcher,
        objs: Iterable[Model],
        field: str,
        related_query: tuple[str | None, QuerySet[Any]],
    ) -> Iterable[Model]:
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
