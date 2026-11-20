from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING, Any

from hare.core.resource_completion import ResourceCompletion
from hare.ddl.table_options import TableOptions
from hare.exceptions import FieldError
from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.fields.enums import RelationLoadStrategy
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.relations.relation_accessors import RelationAccessors
from hare.models.constants import REFRESH_RESTORE_FAILURES_MESSAGE
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.write.instance_snapshot import InstanceSnapshot
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.query.queryset.arguments.collection_arguments import CollectionArguments
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.queryset import QuerySet


class InstanceRefresh:
    """The steps of refresh_from_db(): the query reading the row again, and its values and loaded
    relations carried over to the refreshed obj."""

    @staticmethod
    def get_refresh_field_names(binding: ModelBinding, field_names: Iterable[str]) -> list[str]:
        """Validate the requested field names and prepare the complete list to refresh.

        Args:
            binding: The model.
            field_names: Model field names, any iterable (a generator is consumed here, once).

        Returns:
            The names in order, including blind indexes and every component of requested
            primary keys and relation keys.

        Raises:
            FieldError: If a name is not a column-backed field or a forward relation of this model.
        """
        field_names = CollectionArguments.get_list(field_names, "'fields'")
        validated_names: dict[str, None] = {}
        for field_name in field_names:
            if field_name == PRIMARY_KEY_ALIAS:
                validated_names.update(dict.fromkeys(binding.primary_key_attribute_names))
            elif field_name in binding.fields_db_projection:
                validated_names[field_name] = None
            elif field_name in binding.foreign_key_fields or field_name in binding.one_to_one_fields:
                # A forward relation stands for its key column(s), like Django.
                source_fields = binding.fields_map[field_name].source_fields  # type: ignore[attr-defined]
                validated_names.update(dict.fromkeys(source_fields))
            elif field_name in binding.generic_foreign_key_fields:
                validated_names.update(
                    dict.fromkeys(binding.generic_foreign_key_fields[field_name].get_source_fields())
                )
            elif (
                field_name in binding.backward_foreign_key_fields
                or field_name in binding.backward_one_to_one_fields
                or (field_name in binding.many_to_many_fields)
            ):
                raise FieldError(
                    f"'fields' names the relation {field_name!r} of model {binding.full_name}, "
                    "which has no column on this model's table"
                )
            else:
                raise FieldError(f"Unknown field {field_name!r} in 'fields' for model {binding.full_name}")
        validated_fields = binding.get_with_blind_indexes(validated_names)
        if not validated_fields:
            return validated_fields
        refreshed = dict.fromkeys(validated_fields)
        for relation_name in binding.forward_key_relation_names:
            source_fields = binding.fields_map[relation_name].source_fields  # type: ignore[attr-defined]
            if len(source_fields) > 1 and not refreshed.keys().isdisjoint(source_fields):
                refreshed.update(dict.fromkeys(source_fields))
        for generic_field in binding.generic_foreign_key_fields.values():
            source_fields = generic_field.get_source_fields()
            if not refreshed.keys().isdisjoint(source_fields):
                refreshed.update(dict.fromkeys(source_fields))
        return list(refreshed)

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
        soft_delete_field = obj._binding.soft_delete_field
        is_soft_deleted_in_memory = soft_delete_field is not None and getattr(obj, soft_delete_field, None) is not None
        queryset = TableOptions.get_key_read_queryset(
            RowScopes.get_base_queryset(
                obj._binding,
                RowVisibility(include_deleted=True) if is_soft_deleted_in_memory else RowVisibility.DEFAULT,
            ).using(connection)
        )
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
        meta = obj._binding
        # Field names, not column names - what .only() takes.
        refresh_fields = tuple(fields if fields is not None else meta.fields_db_projection)
        # Read every requested value before publication. A missing column is not a
        # loaded NULL, and must fail before changing the existing object.
        values = [(name, getattr(refreshed_obj, name)) for name in refresh_fields]
        before = InstanceSnapshot.capture_persistence(obj, include_primary_key=False)
        for name in ("_partial", "_await_when_save", "_allow_soft_delete_write"):
            value = obj.__dict__.get(name, ROLLBACK_RESTORE_UNSET)
            before.remember(name, dict(value) if name == "_await_when_save" and isinstance(value, dict) else value)
        # Reverse undo restores key columns before the related objects their setters
        # invalidate. No read participates in database rollback callbacks.
        for name in meta.fetch_fields:
            cache_name = f"_{name}"
            before.remember(cache_name, obj.__dict__.get(cache_name, ROLLBACK_RESTORE_UNSET))
        for name in refresh_fields:
            before.remember(name, getattr(obj, name, ROLLBACK_RESTORE_UNSET))
        _setattr = object.__setattr__
        if meta.soft_delete_field is not None and meta.soft_delete_field in refresh_fields:
            _setattr(obj, "_allow_soft_delete_write", True)
        try:
            for name, value in values:
                setattr(obj, name, value)
            InstanceRefresh.carry_over_forward_relations(obj, refreshed_obj, set(refresh_fields))
            if fields is None:
                InstanceRefresh.reset_reverse_relations(obj, refreshed_obj)
                _setattr(obj, "_partial", False)
            if meta.track_dirty_fields:
                if fields is not None:
                    DirtyFields.sync_dirty_snapshot_fields(obj, refresh_fields)
                else:
                    DirtyFields.snapshot_dirty_fields(obj)
        except BaseException as error:
            with ResourceCompletion.preserve_error(error, REFRESH_RESTORE_FAILURES_MESSAGE):
                InstanceSnapshot.restore_all((before,))
            raise
        finally:
            InstanceSnapshot.restore_value(obj, "_allow_soft_delete_write", before.values["_allow_soft_delete_write"])

    @staticmethod
    def carry_over_forward_relations(obj: Model, refreshed_obj: Model, refresh_fields: set[str]) -> None:
        """Hands the lazy="joined"/"select" relations the refresh query loaded over to the obj -
        setting the key columns dropped its own cached objects.

        Args:
            obj: The obj refreshed.
            refreshed_obj: The row read again.
            refresh_fields: The fields refreshed.
        """
        meta = obj._binding
        for relation_name in meta.forward_key_relation_names:
            relation_field: RelationalField[Any] = meta.fields_map[relation_name]  # type: ignore[assignment]
            if getattr(relation_field, "lazy", None) not in {RelationLoadStrategy.JOINED, RelationLoadStrategy.SELECT}:
                continue
            if refresh_fields.isdisjoint(relation_field.source_fields):
                continue
            cache_key = f"_{relation_name}"
            try:
                cached_value = getattr(refreshed_obj, cache_key)
            except AttributeError:
                if hasattr(obj, cache_key):
                    object.__delattr__(obj, cache_key)
            else:
                object.__setattr__(obj, cache_key, cached_value)

    @staticmethod
    def reset_reverse_relations(obj: Model, refreshed_obj: Model) -> None:
        """Drops the reverse and many-to-many caches of a fully refreshed obj - read again, they
        are fetched again.

        Args:
            obj: The obj refreshed.
            refreshed_obj: The row read again.
        """
        meta = obj._binding
        for relation_name in (
            meta.backward_foreign_key_fields | meta.backward_one_to_one_fields | meta.many_to_many_fields
        ):
            cache_key = f"_{relation_name}"
            if hasattr(obj, cache_key):
                object.__delattr__(obj, cache_key)
        # A lazy="select" many-to-many relation is prefetched onto the row by the refresh query
        # itself - its fresh result is handed to the obj's own relation, just reset.
        for relation_name in meta.many_to_many_fields:
            relation_field: RelationalField[Any] = meta.fields_map[relation_name]  # type: ignore[assignment]
            if getattr(relation_field, "lazy", None) != RelationLoadStrategy.SELECT:
                continue
            refreshed_relation = RelationAccessors.get_relation(refreshed_obj, relation_name)
            if refreshed_relation._fetched:
                relation = RelationAccessors.get_relation(obj, relation_name)
                relation.relation_rows.set_fetched(list(refreshed_relation.related_objects))
