from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any, cast

from hare.fields.enums import RelationLoadStrategy
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.relations.relation_accessors import RelationAccessors
from hare.models.instances.dirty_fields import DirtyFields
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model
    from hare.query.queryset import QuerySet


class InstanceRefresh:
    """The steps of refresh_from_db(): the query reading the row again, and its values and loaded
    relations carried over to the refreshed obj."""

    @staticmethod
    def get_queryset(obj: Model, connection: DatabaseClient, fields: Collection[str] | None) -> QuerySet[Any]:
        """The query reading an obj's row again.

        Args:
            obj: The obj refreshed.
            connection: The connection the row is read on.
            fields: The fields refreshed, None for all.

        Returns:
            The queryset of the model's rows - ``get(pk=...)`` of it reads the row.
        """
        # Read through the base manager with the model's default scope. An obj soft-deleted in
        # memory is refreshed with the soft-delete filter lifted - its row can't be found otherwise.
        soft_delete_field = obj._meta.soft_delete_field
        is_soft_deleted_in_memory = soft_delete_field is not None and getattr(obj, soft_delete_field, None) is not None
        queryset = RowScopes.get_base_queryset(
            obj.__class__, RowVisibility(include_deleted=True) if is_soft_deleted_in_memory else RowVisibility.DEFAULT
        ).using(connection)
        if fields is None:
            return queryset
        # This query's own .only() would skip the JOIN of a lazy="joined" relation -
        # select_related() forces it for the relations being refreshed.
        lazy_joined_relations_needing_refresh = RelationAccessors.get_lazy_joined_relation_names_needing_refresh(
            obj, set(fields)
        )
        if lazy_joined_relations_needing_refresh:
            queryset = queryset.select_related(*lazy_joined_relations_needing_refresh)
        return queryset.only(*fields)

    @staticmethod
    def apply(obj: Model, refreshed_obj: Model, fields: Collection[str] | None) -> None:
        """Carries the values and the loaded relations of a row just read over to an obj.

        Args:
            obj: The obj refreshed.
            refreshed_obj: The row read again.
            fields: The fields refreshed, None for all.
        """
        meta = obj._meta
        # Field names, not column names - what .only() takes.
        refresh_fields = fields if fields is not None else meta.fields_db_projection.keys()
        _setattr = object.__setattr__
        if meta.soft_delete_field is not None and meta.soft_delete_field in refresh_fields:
            _setattr(obj, "_allow_soft_delete_write", True)
        try:
            for field in refresh_fields:
                setattr(obj, field, getattr(refreshed_obj, field, None))
        finally:
            _setattr(obj, "_allow_soft_delete_write", False)
        InstanceRefresh.carry_over_forward_relations(obj, refreshed_obj, set(refresh_fields))
        if not fields:
            InstanceRefresh.reset_reverse_relations(obj, refreshed_obj)
            # A refresh of every field makes a partially loaded obj complete; a refresh of some
            # fields doesn't.
            _setattr(obj, "_partial", False)
        # The refreshed values are the new dirty-tracking baseline.
        if meta.track_dirty_fields:
            if fields:
                DirtyFields.sync_dirty_snapshot_fields(obj, refresh_fields)
            else:
                DirtyFields.snapshot_dirty_fields(obj)

    @staticmethod
    def carry_over_forward_relations(obj: Model, refreshed_obj: Model, refresh_fields: set[str]) -> None:
        """Hands the lazy="joined"/"select" relations the refresh query loaded over to the obj -
        setting the key columns dropped its own cached objects.

        Args:
            obj: The obj refreshed.
            refreshed_obj: The row read again.
            refresh_fields: The fields refreshed.
        """
        meta = obj._meta
        for relation_name in meta.foreign_key_fields | meta.one_to_one_fields:
            relation_field = cast("RelationalField[Any]", meta.fields_map[relation_name])
            if getattr(relation_field, "lazy", None) not in {RelationLoadStrategy.JOINED, RelationLoadStrategy.SELECT}:
                continue
            if not set(relation_field.source_fields) & refresh_fields:
                continue
            cache_key = f"_{relation_name}"
            if hasattr(refreshed_obj, cache_key):
                object.__setattr__(obj, cache_key, getattr(refreshed_obj, cache_key))
            elif hasattr(obj, cache_key):
                object.__delattr__(obj, cache_key)

    @staticmethod
    def reset_reverse_relations(obj: Model, refreshed_obj: Model) -> None:
        """Drops the reverse and many-to-many caches of a fully refreshed obj - read again, they
        are fetched again.

        Args:
            obj: The obj refreshed.
            refreshed_obj: The row read again.
        """
        meta = obj._meta
        for relation_name in (
            meta.backward_foreign_key_fields | meta.backward_one_to_one_fields | meta.many_to_many_fields
        ):
            cache_key = f"_{relation_name}"
            if hasattr(obj, cache_key):
                object.__delattr__(obj, cache_key)
        # A lazy="select" many-to-many relation is prefetched onto the row by the refresh query
        # itself - its fresh result is handed to the obj's own relation, just reset.
        for relation_name in meta.many_to_many_fields:
            relation_field = cast("RelationalField[Any]", meta.fields_map[relation_name])
            if getattr(relation_field, "lazy", None) != RelationLoadStrategy.SELECT:
                continue
            refreshed_relation = RelationAccessors.get_relation(refreshed_obj, relation_name)
            if refreshed_relation._fetched:
                relation = RelationAccessors.get_relation(obj, relation_name)
                relation._set_result_for_query(list(refreshed_relation.related_objects))
