from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.dialects.base.clauses.enums import RowLockStrength
from hare.fields.relations.fields.relational_field import RelationalField
from hare.models.instances.instance_connections import InstanceConnections
from hare.query.queryset.lazy_relation_names import LazyRelationNames
from hare.query.relation_loading.prefetching.direct_relation_prefetch import DirectRelationPrefetch
from hare.query.relation_loading.prefetching.many_to_many_prefetch import ManyToManyPrefetch
from hare.query.relation_loading.prefetching.prefetch import Prefetch
from hare.query.relation_loading.prefetching.prefetch_request import PrefetchRequest
from hare.query.relation_loading.prefetching.reverse_relation_prefetch import ReverseRelationPrefetch

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
        prefetch_queries = {field_name: list(entries) for field_name, entries in prefetch.prefetch_queries.items()}
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
            related_query = RowScopes.get_queryset(related_model, visibility=prefetch.visibility)
            # The connection the parent pinned with .using() is offered to the prefetch query -
            # taken after the router, before the related model's default connection.
            if prefetch.db_explicitly_chosen:
                related_query._router_fallback_connection = self.connection
                # The prefetch query is marked as pinned too, so a nested prefetch gets the same
                # offer.
                related_query._connection_explicitly_chosen = True
            elif (
                related_model._meta.default_connection == self.model._meta.default_connection
                and not InstanceConnections.is_routed(self.model)
            ):
                # The related rows live where the parent rows were read from - looked up by alias
                # at execution time, after the router and before the default connection.
                related_query._instance_connection_alias = self.connection.connection_alias
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
        __, lazy_select_field_names = LazyRelationNames.get(related_queryset.model)
        if lazy_select_field_names <= related_queryset._deferred_related_fields:
            return related_queryset
        related_queryset = related_queryset._clone()
        related_queryset._deferred_related_fields = related_queryset._deferred_related_fields | lazy_select_field_names
        return related_queryset

    async def _do_prefetch(
        self,
        objs: Iterable[Model],
        field: str,
        related_query: tuple[str | None, QuerySet[Any]],
    ) -> None:
        # Nothing returned: the task asyncio.gather() runs this in keeps its result, and a finished
        # task can wait for the garbage collector - the objs would wait with it.
        to_attribute, related_queryset = related_query
        related_query = (to_attribute, self._without_lazy_select_defaults(related_queryset))
        if field in self.model._meta.backward_foreign_key_fields:
            await ReverseRelationPrefetch.prefetch_reverse_relation(self, objs, field, related_query)
        elif field in self.model._meta.backward_one_to_one_fields:
            await ReverseRelationPrefetch.prefetch_reverse_one_to_one_relation(self, objs, field, related_query)
        elif field in self.model._meta.many_to_many_fields:
            await ManyToManyPrefetch.prefetch_many_to_many_relation(self, objs, field, related_query)
        else:
            await DirectRelationPrefetch.prefetch_direct_relation(objs, field, related_query)

    async def prefetch(self, objs: Iterable[Model], prefetch: PrefetchRequest) -> Iterable[Model]:
        if objs and (prefetch.prefetch_map or prefetch.prefetch_queries):
            prefetch_calls = [
                self._do_prefetch(objs, field, related_query)
                for field, related_queries in self._make_prefetch_queries(prefetch).items()
                for related_query in related_queries
            ]
            if len(prefetch_calls) == 1:
                # One prefetch - run as it is, no task made for it.
                await prefetch_calls[0]
            else:
                await asyncio.gather(*prefetch_calls)

        return objs

    async def load(
        self, objs: Iterable[Model], *args: str | Prefetch, db_explicitly_chosen: bool = False
    ) -> Iterable[Model]:
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
