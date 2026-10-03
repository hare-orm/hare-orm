from __future__ import annotations

import asyncio
from collections.abc import Iterable
from functools import partial
from operator import attrgetter
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.constants import PREFETCH_BIND_PARAMS_HEADROOM, PREFETCH_MAX_COMPOSITE_ROWS
from hare.exceptions import QueryError, UnSupportedError
from hare.fields.constants import FK_COLUMN_SUFFIX
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.backward_one_to_one_relation import BackwardOneToOneRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.composite import KeyColumns
from hare.query.constants import PREFETCH_OWNER_KEY_ANNOTATION
from hare.query.enums import Connector
from hare.query.expressions import F, Q
from hare.query.filters.encoders import ValueEncoders
from hare.query.queryset.query_spec import QuerySpec
from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
from hare.query.queryset.relations.reverse_relation import ReverseRelation
from hare.query.relation_loading.prefetch import Prefetch
from hare.query.relation_loading.prefetch_request import PrefetchRequest
from hare.query.rows.hydrate_accelerator import HydrateAccelerator
from hare.query.rows.value_field import ValueField
from hare.sql.queries.tables.table import Table

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset import QuerySet


class Prefetcher:
    """Loads the relations ``prefetch_related()`` asks for onto already loaded instances of one
    model, with one query per relation: a reverse foreign key, a reverse one-to-one, a
    many-to-many or a forward relation, all run concurrently.

    Args:
        model: The instances' model.
        db: The connection the instances were read on.
    """

    __slots__ = ("model", "db")

    def __init__(self, model: type[Model], db: DatabaseClient) -> None:
        self.model = model
        self.db = db

    @staticmethod
    def _reject_prefetch_queryset_limit_or_offset(related_query: QuerySet[Any], field: str) -> None:
        """Rejects a ``Prefetch`` queryset with a limit or offset: it would bound the related rows of
        all parents together, not of each parent.

        Raises:
            UnSupportedError: ``related_query`` is sliced.
        """
        if related_query._limit is not None or related_query._offset is not None:
            raise UnSupportedError(
                f"Prefetch({field!r}, queryset=...) can't combine .limit()/.offset()/slicing "
                "with prefetch_related() - a single query fetches every parent's related rows "
                "together, so the LIMIT/OFFSET would bound that COMBINED result across every "
                "parent instead of each parent's own group, not implement a per-parent limit."
            )

    @staticmethod
    def _reject_m2m_prefetch_queryset_model_mismatch(
        related_queryset: QuerySet[Any], field: str, field_object: ManyToManyFieldInstance[Any]
    ) -> None:
        """Rejects a ``Prefetch`` queryset on a many-to-many relation that isn't of the relation's
        related model.

        Raises:
            QueryError: The queryset is of another model.
        """
        if related_queryset.model is not field_object.related_model:
            raise QueryError(
                f"Prefetch({field!r}, queryset=...) queryset must be built from "
                f"{field_object.related_model.__name__} (this relation's related model), got "
                f"{related_queryset.model.__name__} - a through-model queryset isn't supported here."
            )

    @staticmethod
    def _reject_unsaved_instances(instances: Iterable[Model]) -> None:
        """Checks every instance of ``instances`` is saved - a reverse FK/O2O or M2M relation is
        keyed by the instance's own pk, which an unsaved instance doesn't have yet.

        Raises:
            QueryError: If an instance is not saved.
        """
        for instance in instances:
            if not instance._saved_in_db:
                raise QueryError(f"You should first call .save() on {instance!r}")

    @staticmethod
    def _reject_unloaded_relation_keys(instances: Iterable[Model], field_names: tuple[str, ...], field: str) -> None:
        """Checks every instance loaded the fields the relation ``field`` references it by.

        Raises:
            QueryError: An instance left one of ``field_names`` unloaded (``.only()``/``.defer()``).
        """
        for instance in instances:
            # Only an instance loaded with .only()/.defer() can lack a field.
            if instance._partial:
                instance._get_relation_key_values(field_names, f"Fetching '{field}'")

    @staticmethod
    def _ensure_only_includes_fields(related_queryset: QuerySet[Any], *required_field_names: str) -> QuerySet[Any]:
        """Adds ``required_field_names`` - the columns the prefetch matches rows by - to a ``Prefetch``
        queryset's ``.only()``/``.defer()`` restriction.

        Returns:
            ``related_queryset`` itself when it has no restriction or already selects the fields.
        """
        fields_for_select = related_queryset._fields_for_select
        if fields_for_select and not set(required_field_names) <= set(fields_for_select):
            return related_queryset.only(*fields_for_select, *required_field_names)
        # .defer() fields are expanded only when the query is built - read here directly.
        deferred_fields = related_queryset._deferred_fields
        still_deferred = set(deferred_fields) & set(required_field_names)
        if still_deferred:
            related_queryset = related_queryset._clone()
            related_queryset._deferred_fields = tuple(f for f in deferred_fields if f not in still_deferred)
        return related_queryset

    @staticmethod
    def _collect_related_fetch_values(
        instances: Iterable[Model], related_field_name: str, relation_field: str
    ) -> dict[str, list[Any]]:
        """The ``<relation_field>__in`` values fetching the related rows of ``instances`` - their
        Python values, which the filter encodes for the database itself.

        Args:
            instances: The instances whose related rows are fetched.
            related_field_name: The field of ``instances`` the relation targets.
            relation_field: The related model's key field pointing at it.

        Returns:
            ``relation_field`` to the values.
        """
        values: list[Any] = []
        for instance in instances:
            # A NULL target value (a nullable to_field=) is referenced by no row.
            if (value := getattr(instance, related_field_name)) is not None:
                values.append(value)
        return {relation_field: values}

    @staticmethod
    async def _fetch_matching_any_group(related_queryset: QuerySet[Any], groups: list[Q]) -> list[Model]:
        """Fetches every row of ``related_queryset`` matching any of ``groups``, OR-ing at most
        ``PREFETCH_MAX_COMPOSITE_ROWS`` of them per query.

        Args:
            related_queryset: The prefetch queryset to filter.
            groups: One AND-group per composite key row.

        Returns:
            The fetched rows of every batch, in batch order.
        """
        fetched: list[Model] = []
        for start in range(0, len(groups), PREFETCH_MAX_COMPOSITE_ROWS):
            batch = groups[start : start + PREFETCH_MAX_COMPOSITE_ROWS]
            fetched.extend(await related_queryset.filter(Q.with_connector(Connector.OR, *batch)))
        return fetched

    async def _fetch_by_composite_keys(
        self,
        instances: Iterable[Model],
        related_queryset: QuerySet[Any],
        key_field_names: tuple[str, ...],
        relation_fields: tuple[str, ...],
    ) -> list[Model]:
        """The rows of ``related_queryset`` whose ``relation_fields`` equal the composite key an
        instance holds in ``key_field_names`` - one AND-group per distinct key, OR-ed in batches. A
        key with a NULL part references no row.

        Args:
            instances: The parent instances.
            related_queryset: The related rows' queryset.
            key_field_names: The fields of the parents holding the key.
            relation_fields: The fields of the related rows referencing it.

        Returns:
            The related rows.
        """
        keys: list[tuple[Any, ...]] = []
        seen_keys: set[tuple[Any, ...]] = set()
        for instance in instances:
            key = tuple(getattr(instance, name) for name in key_field_names)
            if None not in key and key not in seen_keys:
                seen_keys.add(key)
                keys.append(key)
        if not keys:
            return []
        related_queryset = self._ensure_only_includes_fields(related_queryset, *relation_fields)
        groups = [Q(**dict(zip(relation_fields, key, strict=True))) for key in keys]
        return await self._fetch_matching_any_group(related_queryset, groups)

    async def _prefetch_reverse_relation(
        self,
        instances: Iterable[Model],
        field: str,
        related_query: tuple[str | None, QuerySet[Any]],
    ) -> Iterable[Model]:
        to_attr, related_query = related_query
        self._reject_prefetch_queryset_limit_or_offset(related_query, field)
        self._reject_unsaved_instances(instances)
        related_field = cast("BackwardFKRelation[Any]", self.model._meta.fields_map[field])
        related_field_names = tuple(f.model_field_name for f in related_field.to_field_instances)
        self._reject_unloaded_relation_keys(instances, related_field_names, field)
        relation_fields = related_field.relation_fields

        if len(relation_fields) == 1:
            related_field_name = related_field_names[0]
            relation_field = relation_fields[0]
            related_objects_for_fetch = self._collect_related_fetch_values(
                instances, related_field_name, relation_field
            )
            related_query = self._ensure_only_includes_fields(related_query, relation_field)
            related_object_list = await related_query.filter(
                **{f"{k}__in": v for k, v in related_objects_for_fetch.items()}
            )

            native_rows = HydrateAccelerator.module
            if native_rows is not None and not to_attr:
                # The rows grouped by their key and handed to each instance's relation in one call
                # each - the loops below.
                native_rows.set_prefetched_rows(
                    instances,
                    related_field_name,
                    native_rows.group_by_attribute(related_object_list, relation_field),
                    f"_{field}",
                    partial(
                        ReverseRelation.get_class_for(related_field.related_model),
                        related_field.related_model,
                        relation_fields,
                        from_fields=related_field_names,
                    ),
                )
                return instances
            related_object_map: dict[Any, list[Model]] = {}
            for entry in related_object_list:
                object_id = getattr(entry, relation_field)
                related_object_map.setdefault(object_id, []).append(entry)
            for instance in instances:
                relation_container = instance._get_relation(field)
                relation_container._set_result_for_query(
                    related_object_map.get(getattr(instance, related_field_name), []),
                    to_attr,
                )
            return instances

        related_object_map = {}
        for entry in await self._fetch_by_composite_keys(
            instances, related_query, related_field_names, relation_fields
        ):
            object_id = tuple(getattr(entry, name) for name in relation_fields)
            related_object_map.setdefault(object_id, []).append(entry)

        for instance in instances:
            relation_container = instance._get_relation(field)
            row = tuple(getattr(instance, name) for name in related_field_names)
            relation_container._set_result_for_query(related_object_map.get(row, []), to_attr)
        return instances

    async def _prefetch_reverse_o2o_relation(
        self,
        instances: Iterable[Model],
        field: str,
        related_query: tuple[str | None, QuerySet[Any]],
    ) -> Iterable[Model]:
        to_attr, related_queryset = related_query
        self._reject_prefetch_queryset_limit_or_offset(related_queryset, field)
        self._reject_unsaved_instances(instances)
        related_field = cast("BackwardOneToOneRelation[Any]", self.model._meta.fields_map[field])
        related_field_names = tuple(f.model_field_name for f in related_field.to_field_instances)
        self._reject_unloaded_relation_keys(instances, related_field_names, field)
        relation_fields = related_field.relation_fields

        related_object_map: dict[Any, Model] = {}
        if len(relation_fields) == 1:
            related_field_name = related_field_names[0]
            relation_field = relation_fields[0]
            related_objects_for_fetch = self._collect_related_fetch_values(
                instances, related_field_name, relation_field
            )
            related_queryset = self._ensure_only_includes_fields(related_queryset, relation_field)
            related_object_list = await related_queryset.filter(
                **{f"{k}__in": v for k, v in related_objects_for_fetch.items()}
            )
            for entry in related_object_list:
                related_object_map[getattr(entry, relation_field)] = entry
            for instance in instances:
                obj = related_object_map.get(getattr(instance, related_field_name), None)
                # A to_attr prefetch leaves the bare relation unfetched - several Prefetch(field,
                # to_attr=...) of one relation don't overwrite each other.
                if to_attr:
                    setattr(instance, to_attr, obj)
                else:
                    setattr(instance, f"_{field}", obj)
            return instances

        for entry in await self._fetch_by_composite_keys(
            instances, related_queryset, related_field_names, relation_fields
        ):
            related_object_map[tuple(getattr(entry, name) for name in relation_fields)] = entry

        for instance in instances:
            row = tuple(getattr(instance, name) for name in related_field_names)
            obj = related_object_map.get(row, None)
            # See the single-column branch above for why to_attr and the bare cache are mutually
            # exclusive here.
            if to_attr:
                setattr(instance, to_attr, obj)
            else:
                setattr(instance, f"_{field}", obj)
        return instances

    async def _prefetch_m2m_relation(
        self,
        instances: Iterable[Model],
        field: str,
        related_query: tuple[str | None, QuerySet[Any]],
    ) -> Iterable[Model]:
        to_attr, related_queryset = related_query
        self._reject_prefetch_queryset_limit_or_offset(related_queryset, field)
        self._reject_unsaved_instances(instances)

        field_object = cast("ManyToManyFieldInstance[Any]", self.model._meta.fields_map[field])
        self._reject_m2m_prefetch_queryset_model_mismatch(related_queryset, field, field_object)
        owner_meta = self.model._meta
        target_meta = related_queryset.model._meta
        owner_pk_fields = owner_meta.pk_fields if owner_meta.has_composite_primary_key else (owner_meta.pk,)
        target_pk_fields = target_meta.pk_fields if target_meta.has_composite_primary_key else (target_meta.pk,)

        through_table = Table(field_object.through, schema=field_object.through_schema)
        backward_columns = [through_table[c] for c in field_object.backward_keys]
        forward_columns = [through_table[c] for c in field_object.forward_keys]

        # The through table lives on the owning side's connection: the owning model's own when it
        # differs or a router decides, else the connection the parent rows were read from.
        owning_model = field_object.related_model if field_object._generated else self.model
        if (
            owning_model._meta.default_connection != self.model._meta.default_connection
            or owning_model._is_routed()
            or (owning_model is not self.model and self.model._is_routed())
        ):
            through_db = owning_model.get_connection(for_write=False)
        else:
            through_db = self.db
        if (
            through_db is self.db
            and HydrateAccelerator.module is not None
            and self.db.features.supports_positional_rows
            and not owner_meta.has_composite_primary_key
            and not target_meta.has_composite_primary_key
            and self._can_join_through_table(related_queryset, field_object)
        ):
            owner_key_values = list(dict.fromkeys(map(attrgetter(owner_meta.pk_attr_names[0]), instances)))
            # More keys than one query binds keep the through-table read, batched.
            if len(owner_key_values) <= self.db.features.max_bind_parameters - PREFETCH_BIND_PARAMS_HEADROOM:
                return await self._prefetch_m2m_by_join(
                    instances, field, to_attr, related_queryset, field_object, owner_key_values
                )
        # The through table is read first, then the related rows by primary key - the same for a
        # single-column and a composite key on either side.
        if owner_meta.has_composite_primary_key:
            instance_pk_db_values = {
                KeyColumns.get_db_values(owner_meta, instance, self.db.dialect.types) for instance in instances
            }
        else:
            # The through table's key column is compared as a filter's __in list is - encoded at once.
            # The key's own column field - a one-to-one primary key's is its key column, not the
            # relation.
            owner_key_name = owner_meta.pk_attr_names[0]
            owner_key_values = list(dict.fromkeys(map(attrgetter(owner_key_name), instances)))
            instance_pk_db_values = {
                (value,)
                for value in ValueEncoders.encode_list(
                    owner_key_values, cast("Model", self.model), owner_meta.fields_map[owner_key_name], self.db.dialect
                )
            }
        through_query = through_db.query_class.from_(through_table).select(*backward_columns, *forward_columns)
        through_model = field_object.through_model_class
        if through_model is not None:
            # The raw through-table SELECT doesn't go through a manager - a through model's own
            # default scope is added here, under the parent query's visibility.
            from hare.query.scopes.row_scopes import RowScopes

            ambient_criterion = RowScopes.of(through_model).get_criterion(
                through_table,
                visibility=related_queryset._visibility,
                dialect=self.db.dialect,
                connection=self.db,
            )
            if ambient_criterion is not None:
                through_query = through_query.where(ambient_criterion)
        # One query per batch of owner keys - every key binds one parameter per column, and the
        # whole list can exceed the backend's own per-statement bind-parameter ceiling.
        instance_pk_value_rows = list(instance_pk_db_values)
        batch_size = max(
            1,
            (through_db.features.max_bind_parameters - PREFETCH_BIND_PARAMS_HEADROOM) // len(backward_columns),
        )
        if len(backward_columns) > 1:
            batch_size = min(batch_size, PREFETCH_MAX_COMPOSITE_ROWS)
        through_rows: list[Any] = []
        for start in range(0, len(instance_pk_value_rows), batch_size):
            # Through the dialect's __in operator - a long list binds as one parameter.
            batch_query = through_query.where(
                through_db.dialect.filter_operators.get_row_membership_criterion(
                    backward_columns, instance_pk_value_rows[start : start + batch_size], owner_pk_fields
                )
            )
            _, batch_rows = await through_db.execute(*batch_query.get_parameterized_sql(), returns_rows=True)
            through_rows.extend(batch_rows)

        native_rows = HydrateAccelerator.module
        if (
            native_rows is not None
            and through_db.features.supports_positional_rows
            and len(owner_pk_fields) == 1
            and len(target_pk_fields) == 1
        ):
            return await self._lay_out_m2m_rows_natively(
                instances, field, to_attr, related_queryset, field_object, through_db, through_rows
            )
        reverse_map: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
        target_pk_python_values_needed: set[tuple[Any, ...]] = set()
        for row in through_rows:
            backward_python_value = tuple(
                through_db.dialect.types.get_python_value(f, row[c])
                for f, c in zip(owner_pk_fields, field_object.backward_keys, strict=True)
            )
            forward_python_value = tuple(
                through_db.dialect.types.get_python_value(f, row[c])
                for f, c in zip(target_pk_fields, field_object.forward_keys, strict=True)
            )
            target_pk_python_values_needed.add(forward_python_value)
            reverse_map.setdefault(forward_python_value, []).append(backward_python_value)

        relation_map: dict[tuple[Any, ...], list[Model]] = {}
        if target_pk_python_values_needed:
            target_pk_attr_names = target_meta.pk_attr_names
            related_queryset = self._ensure_only_includes_fields(related_queryset, *target_pk_attr_names)

            # A single-column target key is filtered by its field name; a composite one by an
            # AND-group per key, OR-ed in batches.
            if len(target_pk_attr_names) == 1:
                related_objects = await related_queryset.filter(
                    **{f"{target_pk_attr_names[0]}__in": [v[0] for v in target_pk_python_values_needed]}
                )
            else:
                groups = [
                    Q(**dict(zip(target_pk_attr_names, row, strict=True))) for row in target_pk_python_values_needed
                ]
                related_objects = await self._fetch_matching_any_group(related_queryset, groups)

            for related_object in related_objects:
                related_pk_tuple = related_object.pk if isinstance(related_object.pk, tuple) else (related_object.pk,)
                for owner_pk_tuple in reverse_map.get(related_pk_tuple, []):
                    relation_map.setdefault(owner_pk_tuple, []).append(related_object)

        for instance in instances:
            instance_pk_tuple = instance.pk if isinstance(instance.pk, tuple) else (instance.pk,)
            relation_container = instance._get_relation(field)
            relation_container._set_result_for_query(relation_map.get(instance_pk_tuple, []), to_attr)
        return instances

    def _can_join_through_table(
        self, related_queryset: QuerySet[Any], field_object: ManyToManyFieldInstance[Any]
    ) -> bool:
        """Whether the related rows of a many-to-many prefetch can be read in one query, joined to
        the through table on the related model's own side of the relation: both are read on the
        parents' connection, the through table has no model of its own (whose default scope the
        raw read adds), and the related queryset neither filters by that relation itself, nor
        annotates, deduplicates or prefetches further - each related row then comes once per owner.

        Args:
            related_queryset: The related rows' queryset.
            field_object: The many-to-many field.

        Returns:
            True when it can.
        """
        target_model = related_queryset.model
        related_name = field_object.related_name
        if (
            field_object.through_model_class is not None
            or not isinstance(target_model._meta.fields_map.get(related_name), ManyToManyFieldInstance)
            # The related rows are read where the through table is.
            or related_queryset.get_connection() is not self.db
            or related_queryset._q_object_list
            or related_queryset._annotations
            or related_queryset._distinct
            or related_queryset._combination is not None
            or related_queryset._prefetch_map
            or related_queryset._prefetch_queries
        ):
            return False
        relation_path_prefix = f"{related_name}__"
        return not any(
            key == related_name or key.startswith(relation_path_prefix)
            for _negate, _generation, kwargs in related_queryset._pending_filter_calls
            for key in kwargs
        )

    async def _prefetch_m2m_by_join(
        self,
        instances: Iterable[Model],
        field: str,
        to_attr: str | None,
        related_queryset: QuerySet[Any],
        field_object: ManyToManyFieldInstance[Any],
        owner_key_values: list[Any],
    ) -> Iterable[Model]:
        """``_prefetch_m2m_relation()`` in one query: the related rows filtered by the relation back
        to the owners - joined to the through table - each with its owner's key, through
        ``rust.native.rows``. A related row of several owners comes once per owner and is one
        instance in each owner's rows.

        Args:
            instances: The instances whose relation is prefetched.
            field: The many-to-many field.
            to_attr: The attribute the related rows are set as, None for the relation itself.
            related_queryset: The related rows' queryset.
            field_object: The field.
            owner_key_values: The instances' keys, each once.

        Returns:
            The instances.
        """
        native_rows = HydrateAccelerator.module
        # Each key by its own column - a one-to-one primary key's is its key column.
        owner_key_name = self.model._meta.pk_attr_names[0]
        target_key_name = related_queryset.model._meta.pk_attr_names[0]
        related_name = field_object.related_name
        related_queryset = self._ensure_only_includes_fields(related_queryset, target_key_name)
        related_rows = await related_queryset.filter(**{f"{related_name}__in": owner_key_values}).annotate(
            **{PREFETCH_OWNER_KEY_ANNOTATION: F(f"{related_name}__{owner_key_name}")}
        )
        related_objects_by_owner = native_rows.group_rows_by_owner(
            related_rows, target_key_name, PREFETCH_OWNER_KEY_ANNOTATION
        )
        if to_attr:
            for instance in instances:
                instance._get_relation(field)._set_result_for_query(
                    related_objects_by_owner.get(getattr(instance, owner_key_name), []), to_attr
                )
            return instances
        native_rows.set_prefetched_rows(
            instances,
            owner_key_name,
            related_objects_by_owner,
            f"_{field}",
            partial(ManyToManyRelation.get_class_for(field_object.related_model), m2m_field=field_object),
        )
        return instances

    async def _lay_out_m2m_rows_natively(
        self,
        instances: Iterable[Model],
        field: str,
        to_attr: str | None,
        related_queryset: QuerySet[Any],
        field_object: ManyToManyFieldInstance[Any],
        through_db: DatabaseClient,
        through_rows: list[Any],
    ) -> Iterable[Model]:
        """The rest of ``_prefetch_m2m_relation()`` for single-column keys on both sides, through
        ``rust.native.rows``: the through rows' keys read by the field codecs, the related rows
        fetched by their keys and laid out on the instances' relations - a call each.

        Args:
            instances: The instances whose relation is prefetched.
            field: The many-to-many field.
            to_attr: The attribute the related rows are set as, None for the relation itself.
            related_queryset: The related rows' queryset.
            field_object: The field.
            through_db: The connection the through rows were read on.
            through_rows: The through rows - the owner's key column, then the target's.

        Returns:
            The instances.
        """
        native_rows = HydrateAccelerator.module
        # Each key by its own column field - a one-to-one primary key's is its key column.
        owner_meta = self.model._meta
        owner_key_name = owner_meta.pk_attr_names[0]
        owner_key_field = owner_meta.fields_map[owner_key_name]
        target_meta = related_queryset.model._meta
        target_key_name = target_meta.pk_attr_names[0]
        target_key_field = target_meta.fields_map[target_key_name]
        owners_by_target_key: dict[Any, list[Any]] = {}
        if through_rows:
            key_reader = HydrateAccelerator.get_values_reader(
                self.model,
                (
                    ValueField(self.model, owner_key_name, owner_key_field, False),
                    ValueField(related_queryset.model, target_key_name, target_key_field, False),
                ),
                through_db,
            )
            owners_by_target_key = native_rows.group_by_item(key_reader.read_tuples(through_rows), 1, 0)
        related_objects: list[Model] = []
        if owners_by_target_key:
            related_queryset = self._ensure_only_includes_fields(related_queryset, target_key_name)
            related_objects = await related_queryset.filter(**{f"{target_key_name}__in": list(owners_by_target_key)})
        related_objects_by_owner = native_rows.group_related(related_objects, target_key_name, owners_by_target_key)
        if to_attr:
            for instance in instances:
                instance._get_relation(field)._set_result_for_query(
                    related_objects_by_owner.get(getattr(instance, owner_key_name), []), to_attr
                )
            return instances
        native_rows.set_prefetched_rows(
            instances,
            owner_key_name,
            related_objects_by_owner,
            f"_{field}",
            partial(ManyToManyRelation.get_class_for(field_object.related_model), m2m_field=field_object),
        )
        return instances

    async def _prefetch_direct_relation(
        self,
        instances: Iterable[Model],
        field: str,
        related_query: tuple[str | None, QuerySet[Any]],
    ) -> Iterable[Model]:
        to_attr, related_queryset = related_query
        self._reject_prefetch_queryset_limit_or_offset(related_queryset, field)

        # Checked once per source class: when every class targets a single column, the dict-based
        # path below runs.
        source_fields_by_class: dict[type[Model], tuple[str, ...]] = {}
        for instance in instances:
            if instance.__class__ not in source_fields_by_class:
                related_field = cast("RelationalField[Any]", instance._meta.fields_map[field])
                source_fields_by_class[instance.__class__] = related_field.source_fields

        if all(len(names) == 1 for names in source_fields_by_class.values()):
            related_objects_for_fetch: dict[str, list[Any]] = {}
            seen_values_for_fetch: dict[str, set[Any]] = {}
            relation_key_field = f"{field}{FK_COLUMN_SUFFIX}"
            model_to_field: dict[type[Model], str] = {}
            for instance in instances:
                if (value := getattr(instance, relation_key_field)) is not None:
                    if (model_cls := instance.__class__) in model_to_field:
                        key = model_to_field[model_cls]
                    else:
                        related_field = cast("RelationalField[Any]", instance._meta.fields_map[field])
                        key = related_field.to_field_names[0]
                        model_to_field[model_cls] = key
                        if key not in related_objects_for_fetch:
                            related_objects_for_fetch[key] = []
                            seen_values_for_fetch[key] = set()
                    seen = seen_values_for_fetch[key]
                    if value not in seen:
                        seen.add(value)
                        related_objects_for_fetch[key].append(value)
                else:
                    # These instances have no related row - their to_attr is set to None here, as
                    # the loop below never reaches them.
                    if to_attr:
                        setattr(instance, to_attr, None)
                    else:
                        setattr(instance, field, None)

            if related_objects_for_fetch:
                conditions: dict[str, Any] = {}
                for k, v in related_objects_for_fetch.items():
                    if len(v) == 1:
                        v = v[0]
                    else:
                        k += "__in"
                    conditions[k] = v
                related_queryset = self._ensure_only_includes_fields(
                    related_queryset, *related_objects_for_fetch.keys()
                )
                related_object_list = await related_queryset.filter(**conditions)
                multi_model = len(model_to_field) > 1
                if multi_model:
                    # Keyed by (to_field name, value): source classes may reference the related
                    # model through different to_field columns, and each instance is matched by its
                    # own class's.
                    related_object_map = {
                        (to_field, getattr(obj, to_field)): obj
                        for to_field in set(model_to_field.values())
                        for obj in related_object_list
                    }
                else:
                    related_object_map = {getattr(obj, key): obj for obj in related_object_list}
                for instance in instances:
                    if multi_model:
                        to_field = model_to_field.get(instance.__class__)
                        obj = (
                            related_object_map.get((to_field, getattr(instance, relation_key_field)))
                            if to_field
                            else None
                        )
                    else:
                        obj = related_object_map.get(getattr(instance, relation_key_field))
                    # See ReverseRelation._set_result_for_query()'s own docstring for why to_attr
                    # and the bare field/cache are mutually exclusive here.
                    if to_attr:
                        setattr(instance, to_attr, obj)
                    else:
                        self._set_prefetched_fk_object(instance, field, obj)
            return instances

        # A composite target: an OR of AND-groups instead of the single-column conditions above, per
        # source class.
        to_field_names_by_class: dict[type[Model], tuple[str, ...]] = {}
        rows_by_class: dict[type[Model], list[tuple[Any, ...]]] = {}
        seen_rows_by_class: dict[type[Model], set[tuple[Any, ...]]] = {}
        for instance in instances:
            model_cls = instance.__class__
            source_fields = source_fields_by_class[model_cls]
            row = tuple(getattr(instance, sf) for sf in source_fields)
            if any(component is None for component in row):
                # See the single-column branch above for why to_attr needs its own None here too.
                if to_attr:
                    setattr(instance, to_attr, None)
                else:
                    setattr(instance, field, None)
                continue
            if model_cls not in to_field_names_by_class:
                related_field = cast("RelationalField[Any]", model_cls._meta.fields_map[field])
                to_field_names_by_class[model_cls] = tuple(
                    f.model_field_name for f in related_field.to_field_instances
                )
            rows = rows_by_class.setdefault(model_cls, [])
            seen_rows = seen_rows_by_class.setdefault(model_cls, set())
            if row not in seen_rows:
                seen_rows.add(row)
                rows.append(row)

        if rows_by_class:
            distinct_to_field_shapes = set(to_field_names_by_class.values())
            multi_shape = len(distinct_to_field_shapes) > 1
            groups = [
                Q(**dict(zip(to_field_names_by_class[model_cls], row, strict=True)))
                for model_cls, rows in rows_by_class.items()
                for row in rows
            ]
            related_queryset = self._ensure_only_includes_fields(
                related_queryset, *{name for names in to_field_names_by_class.values() for name in names}
            )
            related_object_list = await self._fetch_matching_any_group(related_queryset, groups)

            composite_related_object_map: dict[Any, Model] = {}
            for obj in related_object_list:
                for to_field_names in distinct_to_field_shapes:
                    key_row = tuple(getattr(obj, name) for name in to_field_names)
                    map_key = (to_field_names, key_row) if multi_shape else key_row
                    composite_related_object_map[map_key] = obj

            for instance in instances:
                model_cls = instance.__class__
                source_fields = source_fields_by_class[model_cls]
                row = tuple(getattr(instance, sf) for sf in source_fields)
                if any(component is None for component in row):
                    continue
                to_field_names = to_field_names_by_class[model_cls]
                map_key = (to_field_names, row) if multi_shape else row
                obj = composite_related_object_map.get(map_key)
                # See the single-column branch above for why to_attr and the bare field/cache are
                # mutually exclusive here.
                if to_attr:
                    setattr(instance, to_attr, obj)
                else:
                    self._set_prefetched_fk_object(instance, field, obj)
        return instances

    @staticmethod
    def _set_prefetched_fk_object(instance: Model, field: str, related_object: Model | None) -> None:
        """Stores a prefetched forward relation's object on an instance. A missing target is cached as
        None without the relation's setter, which would also set the instance's key column to None.

        Args:
            instance: The instance the relation was prefetched for.
            field: The relation field's name.
            related_object: The fetched target, None when no row was found.
        """
        if related_object is None:
            setattr(instance, f"_{field}", None)
        else:
            setattr(instance, field, related_object)

    def _make_prefetch_queries(self, prefetch: PrefetchRequest) -> dict[str, list[tuple[str | None, QuerySet[Any]]]]:
        """The query of each prefetch, by relation name: every explicit ``Prefetch``, and the plain
        relation's query wherever a lookup goes through it, with the nested lookups. The request
        itself is left as it is.

        Args:
            prefetch: The request.

        Returns:
            ``(to_attr, queryset)`` of each prefetch, by relation name.
        """
        prefetch_queries = {field_name: list(entries) for field_name, entries in prefetch.prefetch_queries.items()}
        for field_name, forwarded_prefetches in prefetch.prefetch_map.items():
            entries = prefetch_queries.setdefault(field_name, [])
            # The plain relation's own Prefetch(field_name, queryset) takes the nested lookups;
            # one with to_attr= loads another attribute and keeps only its own.
            plain_index = next((index for index, (to_attr, _query) in enumerate(entries) if to_attr is None), None)
            if plain_index is not None:
                if forwarded_prefetches:
                    entries[plain_index] = (None, entries[plain_index][1].prefetch_related(*forwarded_prefetches))
                continue

            relation_field = cast("RelationalField[Any]", self.model._meta.fields_map[field_name])
            related_model: type[Model] = relation_field.related_model
            # A queryset whose connection isn't chosen yet - it picks its own when it runs, the
            # fallback below included. It gets the parent query's visibility and tenant: the related
            # rows are scoped to the tenant the parent query was built under.
            from hare.query.scopes.row_scopes import RowScopes

            related_query = RowScopes.get_queryset(related_model, visibility=prefetch.visibility)
            # The connection the parent pinned with .using() is offered to the prefetch query -
            # taken after the router, before the related model's default connection.
            if prefetch.db_explicitly_chosen:
                related_query._router_fallback_db = self.db
                # The prefetch query is marked as pinned too, so a nested prefetch gets the same
                # offer.
                related_query._db_explicitly_chosen = True
            elif (
                related_model._meta.default_connection == self.model._meta.default_connection
                and not self.model._is_routed()
            ):
                # The related rows live where the parent rows were read from - looked up by alias
                # at execution time, after the router and before the default connection.
                related_query._instance_connection_name = self.db.connection_name
            # The parent's select_for_update() locks the prefetched rows too. `of=` names the parent
            # query's tables and isn't passed on.
            if prefetch.select_for_update:
                related_query = related_query.select_for_update(
                    nowait=prefetch.select_for_update_nowait,
                    skip_locked=prefetch.select_for_update_skip_locked,
                    no_key=prefetch.select_for_update_no_key,
                )
            if forwarded_prefetches:
                related_query = related_query.prefetch_related(*forwarded_prefetches)
            # Appended - the relation may also have Prefetch(..., to_attr=...) entries of its own.
            entries.append((None, related_query))
        return prefetch_queries

    @staticmethod
    def _without_lazy_select_defaults(related_queryset: QuerySet[Any]) -> QuerySet[Any]:
        """Opts a prefetch query out of its model's ``lazy=RelationLoadStrategy.SELECT`` defaults: a
        prefetch loads one level - a self-referential relation would otherwise walk the whole chain.
        Relations prefetched explicitly are unaffected.

        Args:
            related_queryset: The query a prefetch is about to run.

        Returns:
            ``related_queryset`` itself when there is nothing to opt out of, else a clone.
        """
        __, lazy_select_field_names = QuerySpec.get_lazy_relation_names(related_queryset.model)
        if lazy_select_field_names <= related_queryset._deferred_related_fields:
            return related_queryset
        related_queryset = related_queryset._clone()
        related_queryset._deferred_related_fields = related_queryset._deferred_related_fields | lazy_select_field_names
        return related_queryset

    async def _do_prefetch(
        self,
        instances: Iterable[Model],
        field: str,
        related_query: tuple[str | None, QuerySet[Any]],
    ) -> Iterable[Model]:
        to_attr, related_queryset = related_query
        related_query = (to_attr, self._without_lazy_select_defaults(related_queryset))
        if field in self.model._meta.backward_fk_fields:
            return await self._prefetch_reverse_relation(instances, field, related_query)

        if field in self.model._meta.backward_o2o_fields:
            return await self._prefetch_reverse_o2o_relation(instances, field, related_query)

        if field in self.model._meta.m2m_fields:
            return await self._prefetch_m2m_relation(instances, field, related_query)
        return await self._prefetch_direct_relation(instances, field, related_query)

    async def prefetch(self, instances: Iterable[Model], prefetch: PrefetchRequest) -> Iterable[Model]:
        if instances and (prefetch.prefetch_map or prefetch.prefetch_queries):
            prefetch_tasks = []
            for field, related_queries in self._make_prefetch_queries(prefetch).items():
                for related_query in related_queries:
                    prefetch_tasks.append(self._do_prefetch(instances, field, related_query))
            await asyncio.gather(*prefetch_tasks)

        return instances

    async def load(
        self, instances: Iterable[Model], *args: str | Prefetch, db_explicitly_chosen: bool = False
    ) -> Iterable[Model]:
        prefetch_map: dict[str, set[str | Prefetch]] = {}
        prefetch_queries: dict[str, list[tuple[str | None, QuerySet[Any]]]] = {}
        Prefetch.add_lookups(self.model, prefetch_map, prefetch_queries, args)
        if instances and (prefetch_map or prefetch_queries):
            await self.prefetch(
                instances, PrefetchRequest(prefetch_map, prefetch_queries, db_explicitly_chosen=db_explicitly_chosen)
            )
        return instances
