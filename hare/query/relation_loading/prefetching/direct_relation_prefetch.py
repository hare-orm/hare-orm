from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from hare.fields.constants import FOREIGN_KEY_COLUMN_SUFFIX
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.expressions import Q
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks
from hare.query.relation_loading.prefetching.related_rows_fetch import RelatedRowsFetch
from hare.query.relation_loading.prefetching.sliced_prefetch import SlicedPrefetch

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset import QuerySet


class DirectRelationPrefetch:
    """The prefetch of a forward foreign key or one-to-one relation: the related rows fetched by the
    objs' key values and set on each obj."""

    @staticmethod
    async def prefetch_direct_relation(
        objs: Iterable[Model],
        field: str,
        related_query: tuple[str | None, QuerySet[Any]],
    ) -> Iterable[Model]:
        """Fetches the related rows of a forward relation and sets them on the objs.

        Args:
            objs: The objs whose relation is prefetched.
            field: The relation field's name.
            related_query: The attribute the related row is set as (None for the relation itself)
                and the related rows' queryset.

        Returns:
            The objs.
        """
        to_attribute, related_queryset = related_query
        if SlicedPrefetch.is_sliced(related_queryset):
            if not SlicedPrefetch.keeps_the_only_row(related_queryset):
                # A parent has one row at most - a slice past it leaves none.
                for obj in objs:
                    if to_attribute:
                        setattr(obj, to_attribute, None)
                    else:
                        DirectRelationPrefetch.set_prefetched_foreign_key_object(obj, field, None)
                return objs
            related_queryset = SlicedPrefetch.get_unsliced(related_queryset)

        source_fields_by_class: dict[type[Model], tuple[str, ...]] = {}
        for obj in objs:
            if obj.__class__ not in source_fields_by_class:
                related_field = cast("RelationalField[Any]", obj._meta.fields_map[field])
                source_fields_by_class[obj.__class__] = related_field.source_fields
        if all(len(names) == 1 for names in source_fields_by_class.values()):
            await DirectRelationPrefetch.prefetch_by_single_column(objs, field, to_attribute, related_queryset)
        else:
            await DirectRelationPrefetch.prefetch_by_composite_key(
                objs, field, to_attribute, related_queryset, source_fields_by_class
            )
        return objs

    @staticmethod
    async def prefetch_by_single_column(
        objs: Iterable[Model], field: str, to_attribute: str | None, related_queryset: QuerySet[Any]
    ) -> None:
        """The prefetch of a relation every source class keeps in one key column.

        Args:
            objs: The objs whose relation is prefetched.
            field: The relation field's name.
            to_attribute: The attribute the related row is set as, None for the relation itself.
            related_queryset: The related rows' queryset.
        """
        relation_key_field = f"{field}{FOREIGN_KEY_COLUMN_SUFFIX}"
        target_field_by_class: dict[type[Model], str] = {}
        key_values_by_target_field: dict[str, dict[Any, None]] = {}
        for obj in objs:
            value = getattr(obj, relation_key_field)
            if value is None:
                # No related row - set here, the fetched rows never reach this obj's attribute.
                setattr(obj, to_attribute or field, None)
                continue
            target_field = target_field_by_class.get(obj.__class__)
            if target_field is None:
                related_field = cast("RelationalField[Any]", obj._meta.fields_map[field])
                target_field = target_field_by_class[obj.__class__] = related_field.to_field_names[0]
            key_values = key_values_by_target_field.get(target_field)
            if key_values is None:
                key_values = key_values_by_target_field[target_field] = {}
            key_values[value] = None
        if not key_values_by_target_field:
            return

        conditions: dict[str, Any] = {}
        for target_field, key_values in key_values_by_target_field.items():
            if len(key_values) == 1:
                conditions[target_field] = next(iter(key_values))
            else:
                conditions[f"{target_field}__in"] = list(key_values)
        related_queryset = PrefetchChecks.ensure_only_includes_fields(related_queryset, *key_values_by_target_field)
        related_objects = await related_queryset.filter(**conditions)
        if len(target_field_by_class) > 1:
            DirectRelationPrefetch.set_objects_of_several_classes(
                objs, field, to_attribute, related_objects, target_field_by_class
            )
            return
        (target_field,) = key_values_by_target_field
        related_object_by_key = {
            getattr(related_object, target_field): related_object for related_object in related_objects
        }
        for obj in objs:
            related_object = related_object_by_key.get(getattr(obj, relation_key_field))
            # The attribute and the relation itself exclude each other: a prefetch into an attribute
            # leaves the relation unloaded.
            if to_attribute:
                setattr(obj, to_attribute, related_object)
            else:
                DirectRelationPrefetch.set_prefetched_foreign_key_object(obj, field, related_object)

    @staticmethod
    def set_objects_of_several_classes(
        objs: Iterable[Model],
        field: str,
        to_attribute: str | None,
        related_objects: Iterable[Model],
        target_field_by_class: dict[type[Model], str],
    ) -> None:
        """Sets the fetched rows on objs of several classes - the classes may reference the related
        model through different target fields, and each obj is matched by its own class's.

        Args:
            objs: The objs whose relation is prefetched.
            field: The relation field's name.
            to_attribute: The attribute the related row is set as, None for the relation itself.
            related_objects: The fetched rows.
            target_field_by_class: The target field of each source class that has a related row.
        """
        relation_key_field = f"{field}{FOREIGN_KEY_COLUMN_SUFFIX}"
        related_object_by_key = {
            (target_field, getattr(related_object, target_field)): related_object
            for target_field in set(target_field_by_class.values())
            for related_object in related_objects
        }
        for obj in objs:
            target_field = target_field_by_class.get(obj.__class__)
            related_object = (
                related_object_by_key.get((target_field, getattr(obj, relation_key_field))) if target_field else None
            )
            if to_attribute:
                setattr(obj, to_attribute, related_object)
            else:
                DirectRelationPrefetch.set_prefetched_foreign_key_object(obj, field, related_object)

    @staticmethod
    async def prefetch_by_composite_key(
        objs: Iterable[Model],
        field: str,
        to_attribute: str | None,
        related_queryset: QuerySet[Any],
        source_fields_by_class: dict[type[Model], tuple[str, ...]],
    ) -> None:
        """The prefetch of a relation to a composite key: the related rows matched by an OR of
        AND-groups, per source class.

        Args:
            objs: The objs whose relation is prefetched.
            field: The relation field's name.
            to_attribute: The attribute the related row is set as, None for the relation itself.
            related_queryset: The related rows' queryset.
            source_fields_by_class: The key fields of the relation in each source class.
        """
        to_field_names_by_class: dict[type[Model], tuple[str, ...]] = {}
        rows_by_class: dict[type[Model], dict[tuple[Any, ...], None]] = {}
        for obj in objs:
            model_class = obj.__class__
            row = tuple(getattr(obj, source_field) for source_field in source_fields_by_class[model_class])
            if any(component is None for component in row):
                # No related row - set here, the fetched rows never reach this obj's attribute.
                setattr(obj, to_attribute or field, None)
                continue
            if model_class not in to_field_names_by_class:
                related_field = cast("RelationalField[Any]", model_class._meta.fields_map[field])
                to_field_names_by_class[model_class] = tuple(
                    target_field.model_field_name for target_field in related_field.to_field_instances
                )
            rows_by_class.setdefault(model_class, {})[row] = None
        if not rows_by_class:
            return

        distinct_to_field_shapes = set(to_field_names_by_class.values())
        multi_shape = len(distinct_to_field_shapes) > 1
        groups = [
            Q(**dict(zip(to_field_names_by_class[model_class], row, strict=True)))
            for model_class, rows in rows_by_class.items()
            for row in rows
        ]
        related_queryset = PrefetchChecks.ensure_only_includes_fields(
            related_queryset, *{name for names in to_field_names_by_class.values() for name in names}
        )
        related_object_by_key: dict[Any, Model] = {}
        for fetched_object in await RelatedRowsFetch.fetch_matching_any_group(related_queryset, groups):
            for to_field_names in distinct_to_field_shapes:
                key_row = tuple(getattr(fetched_object, name) for name in to_field_names)
                related_object_by_key[(to_field_names, key_row) if multi_shape else key_row] = fetched_object

        for obj in objs:
            model_class = obj.__class__
            row = tuple(getattr(obj, source_field) for source_field in source_fields_by_class[model_class])
            if any(component is None for component in row):
                continue
            related_object = related_object_by_key.get(
                (to_field_names_by_class[model_class], row) if multi_shape else row
            )
            if to_attribute:
                setattr(obj, to_attribute, related_object)
            else:
                DirectRelationPrefetch.set_prefetched_foreign_key_object(obj, field, related_object)

    @staticmethod
    def set_prefetched_foreign_key_object(obj: Model, field: str, related_object: Model | None) -> None:
        """Stores a prefetched forward relation's object on an obj. A missing target is cached as
        None without the relation's setter, which would also set the obj's key column to None.

        Args:
            obj: The obj the relation was prefetched for.
            field: The relation field's name.
            related_object: The fetched target, None when no row was found.
        """
        if related_object is None:
            setattr(obj, f"_{field}", None)
        else:
            setattr(obj, field, related_object)
