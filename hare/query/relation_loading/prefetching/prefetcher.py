from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from hare.dialects.base.clauses.enums import RowLockStrength
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.relation_loading.prefetching.declarations import PrefetchNode
from hare.query.relation_loading.prefetching.direct_relation_prefetch import DirectRelationPrefetch
from hare.query.relation_loading.prefetching.enums import PrefetchRowsStrategy, PrefetchStrategy
from hare.query.relation_loading.prefetching.many_to_many_prefetch import ManyToManyPrefetch
from hare.query.relation_loading.prefetching.prefetch import Prefetch
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks
from hare.query.relation_loading.prefetching.prefetch_plan import PrefetchPlan
from hare.query.relation_loading.prefetching.prefetch_request import PrefetchRequest
from hare.query.relation_loading.prefetching.prefetch_result import PrefetchResult
from hare.query.relation_loading.prefetching.reverse_relation_prefetch import ReverseRelationPrefetch

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset import QuerySet


class Prefetcher:
    """Loads the relations ``prefetch_related()`` asks for onto already loaded instances of one
    model. Independent relations are fetched concurrently; each can require multiple queries for
    through-table reads, connection boundaries or key batches. Attachments are published together.

    Args:
        model: The instances' model.
        connection: The connection the instances were read on.
    """

    __slots__ = ("model", "connection")

    def __init__(self, model: type[Model], connection: DatabaseClient) -> None:
        self.model = model
        self.connection = connection

    def _make_prefetch_queries(self, prefetch: PrefetchRequest) -> dict[str, list[tuple[str | None, QuerySet[Any]]]]:
        """The query of each prefetch, by relation name: every explicit ``Prefetch``, and the plain
        relation's query wherever a lookup goes through it, with the nested lookups. The request
        itself is left as it is.

        Args:
            prefetch: The request.

        Returns:
            ``(to_attribute, queryset)`` of each prefetch, by relation name.
        """
        # A Prefetch queryset with no connection of its own gets the parent's offer, as a plain relation does.
        prefetch_queries = {
            field_name: [
                (
                    to_attribute,
                    query._clone()
                    if query._connection_explicitly_chosen
                    else self._with_parent_connection(query._clone(), prefetch),
                )
                for to_attribute, query in entries
            ]
            for field_name, entries in prefetch.prefetch_queries.items()
        }
        for field_name, forwarded_prefetches in prefetch.prefetch_map.items():
            entries = prefetch_queries.setdefault(field_name, [])
            # The plain relation's own Prefetch(field_name, queryset) takes the nested lookups;
            # one with to_attribute= loads another attribute and keeps only its own.
            plain_index = next(
                (index for index, (to_attribute, _query) in enumerate(entries) if to_attribute is None), None
            )
            if plain_index is not None:
                if forwarded_prefetches:
                    entries[plain_index] = (None, entries[plain_index][1].prefetch_related(*forwarded_prefetches))
                continue

            relation_field: RelationalField[Any] = self.model._meta.fields_map[field_name]  # type: ignore[assignment]
            related_model: type[Model] = relation_field.related_model
            # A queryset whose connection isn't chosen yet - it picks its own when it runs, the
            # fallback below included. It gets the parent query's visibility and tenant: the related
            # rows are scoped to the tenant the parent query was built under.
            related_query = self._with_parent_connection(
                RowScopes.get_queryset(related_model, visibility=prefetch.visibility)._clone(), prefetch
            )
            # The parent's select_for_update() locks the prefetched rows too. `of=` names the parent
            # query's tables and isn't passed on.
            if prefetch.select_for_update:
                related_query = related_query.select_for_update(
                    nowait=prefetch.select_for_update_nowait,
                    skip_locked=prefetch.select_for_update_skip_locked,
                    no_key=prefetch.select_for_update_strength is RowLockStrength.NO_KEY_UPDATE,
                    share=prefetch.select_for_update_strength is RowLockStrength.SHARE,
                    key_share=prefetch.select_for_update_strength is RowLockStrength.KEY_SHARE,
                )
            if forwarded_prefetches:
                related_query = related_query.prefetch_related(*forwarded_prefetches)
            # Appended - the relation may also have Prefetch(..., to_attribute=...) entries of its own.
            entries.append((None, related_query))
        return prefetch_queries

    def _with_parent_connection(self, related_query: QuerySet[Any], prefetch: PrefetchRequest) -> QuerySet[Any]:
        """Offers the parent query's connection to a prefetch query that hasn't chosen one.

        Args:
            related_query: The prefetch query - changed in place.
            prefetch: The request.

        Returns:
            ``related_query``.
        """
        # A related model of another default connection (a cross-database relation) keeps its own
        # connection choice - the parent's is no database of its rows.
        if related_query.model._meta.default_connection != self.model._meta.default_connection:
            return related_query
        # The connection the parent pinned with .using() is offered to the prefetch query -
        # taken after the router, before the related model's default connection.
        if prefetch.db_explicitly_chosen:
            related_query._router_fallback_connection = self.connection
            # The prefetch query is marked as pinned too, so a nested prefetch gets the same
            # offer.
            related_query._connection_explicitly_chosen = True
        else:
            # The related rows live where the parent rows were read from - looked up by alias
            # at execution time, after the router and before the default connection.
            related_query._instance_connection_alias = self.connection.connection_alias
            related_query._instance_connection_model = self.model
        return related_query

    async def _fetch_node(self, objs: Iterable[Model], node: PrefetchNode, request: PrefetchRequest) -> PrefetchResult:
        """Fetches one prepared node into a detached result."""
        if node.strategy is PrefetchStrategy.LOADED:
            return await self.prefetch_through_loaded_relation(objs, node.field, request)
        result = PrefetchResult()
        if node.rows_strategy is PrefetchRowsStrategy.EMPTY:
            for obj in objs:
                if node.strategy is PrefetchStrategy.DIRECT:
                    result.add_forward(obj, node.field, node.to_attribute, None)
                else:
                    result.add_reverse(obj, node.field, node.to_attribute, None)
            return result
        # Loaded and empty nodes returned above; every remaining fetch has a prepared query.
        assert node.queryset is not None  # nosec B101
        if node.strategy is PrefetchStrategy.REVERSE:
            await ReverseRelationPrefetch.prefetch_reverse_relation(objs, node, result)
        elif node.strategy is PrefetchStrategy.REVERSE_SINGLE:
            await ReverseRelationPrefetch.prefetch_reverse_one_to_one_relation(objs, node, result)
        elif node.strategy is PrefetchStrategy.MANY_TO_MANY:
            await ManyToManyPrefetch.prefetch_many_to_many_relation(self, objs, node, result)
        else:
            await DirectRelationPrefetch.prefetch_direct_relation(objs, node, result)
        if node.row_slice is not None:
            result.take_slice(node.row_slice)
        if node.dependent_request is not None:
            related_by_id = {}
            for attachment in result.attachments:
                value = attachment.value
                values = value if isinstance(value, list) else () if value is None else (value,)
                for obj in values:
                    related_by_id[id(obj)] = obj
            # A dependent request belongs to the connection selected during node preparation.
            assert node.target_connection is not None  # nosec B101
            dependent_plan = PrefetchPlan.prepare(
                Prefetcher(node.queryset.model, node.target_connection),
                related_by_id.values(),
                node.dependent_request,
            )
            result.extend(await dependent_plan.fetch())
        return result

    async def prefetch(self, objs: Iterable[Model], prefetch: PrefetchRequest) -> Iterable[Model]:
        """Binds and fetches all requested relations, then attaches successful results together."""
        plan = PrefetchPlan.prepare(self, objs, prefetch)
        result = await plan.fetch()
        result.attach()
        return plan.objs

    def get_loaded_relation_names(self, objs: Iterable[Model], prefetch: PrefetchRequest) -> list[str]:
        """The forward relations a prefetch goes through that every obj has loaded already - by
        ``select_related()``, or with no related row to load - and no ``Prefetch()`` of its own reads.

        Args:
            objs: The objects.
            prefetch: The request.

        Returns:
            The relations' names.
        """
        meta = self.model._meta
        forward_relation_names = meta.foreign_key_fields | meta.one_to_one_fields
        loaded_names = []
        for field_name, forwarded in prefetch.prefetch_map.items():
            if not forwarded or field_name not in forward_relation_names or field_name in prefetch.prefetch_queries:
                continue
            source_fields = meta.fields_map[field_name].source_fields  # type: ignore[attr-defined]
            for obj in objs:
                if f"_{field_name}" in vars(obj):
                    continue
                if obj._partial:
                    PrefetchChecks.reject_unloaded_relation_keys((obj,), source_fields, field_name)
                if any(getattr(obj, name) is not None for name in source_fields):
                    break
            else:
                loaded_names.append(field_name)
        return loaded_names

    async def prefetch_through_loaded_relation(
        self, objs: Iterable[Model], field: str, prefetch: PrefetchRequest
    ) -> PrefetchResult:
        """Runs the prefetches nested in a loaded forward relation on its loaded objects - the
        relation itself isn't read again, which would replace them.

        Args:
            objs: The objects.
            field: The relation's name.
            prefetch: The request.
        """
        result = PrefetchResult()
        related_by_id: dict[int, Model] = {}
        for obj in objs:
            if f"_{field}" not in vars(obj):
                # A key of no related row - loaded as None, like a prefetch of the relation itself.
                result.add_forward(obj, field, None, None)
            elif (related := vars(obj)[f"_{field}"]) is not None:
                related_by_id[id(related)] = related
        related_objs = list(related_by_id.values())
        if not related_objs:
            return result
        related_model = self.model._meta.fields_map[field].related_model  # type: ignore[attr-defined]
        prefetch_map: dict[str, set[str | Prefetch]] = {}
        prefetch_queries: dict[str, list[tuple[str | None, QuerySet[Any]]]] = {}
        Prefetch.add_lookups(related_model, prefetch_map, prefetch_queries, prefetch.prefetch_map[field])
        plan = PrefetchPlan.prepare(
            Prefetcher(related_model, self.connection),
            related_objs,
            replace(prefetch, prefetch_map=prefetch_map, prefetch_queries=prefetch_queries),
        )
        result.extend(await plan.fetch())
        return result

    async def load(
        self, objs: Iterable[Model], *args: str | Prefetch, db_explicitly_chosen: bool = False
    ) -> Iterable[Model]:
        """Loads the relations ``prefetch_related_objects()`` names onto the objects.

        Args:
            objs: The objects.
            args: The relation paths and ``Prefetch`` objects.
            db_explicitly_chosen: Whether the objects' connection was chosen by ``using()``.

        Returns:
            The objects.
        """
        prefetch_map: dict[str, set[str | Prefetch]] = {}
        prefetch_queries: dict[str, list[tuple[str | None, QuerySet[Any]]]] = {}
        Prefetch.add_lookups(self.model, prefetch_map, prefetch_queries, args)
        if objs and (prefetch_map or prefetch_queries):
            await self.prefetch(
                objs, PrefetchRequest(prefetch_map, prefetch_queries, db_explicitly_chosen=db_explicitly_chosen)
            )
        return objs


# Imported last: the scopes import the prefetch modules.
from hare.query.scopes.row_scopes import RowScopes  # noqa: E402
