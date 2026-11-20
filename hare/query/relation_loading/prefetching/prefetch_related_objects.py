from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.exceptions import QueryError
from hare.models.instances.instance_connections import InstanceConnections
from hare.query.execution.query_execution import QueryExecution

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.queryset import QuerySet
    from hare.query.relation_loading.prefetching.prefetch import Prefetch


async def prefetch_related_objects(
    objs: Iterable[Model], *lookups: str | Prefetch, using: str | DatabaseClient | None = None
) -> None:
    """Loads relations of objs already in hand, as ``prefetch_related()`` does for the rows
    of a queryset - one query per relation for all of them.

    .. code-block:: python3

        await prefetch_related_objects(users, "emails", Prefetch("posts", Post.objects.filter(published=True)))

    Args:
        objs: Instances - of several models too, every lookup starting on each of them.
        lookups: Relation paths (``"posts__comments"``), or ``Prefetch(...)`` for a custom
            queryset or ``to_attribute``.
        using: Connection to read through instead of the one the first instance came from.

    Raises:
        FieldError: A lookup starts with neither a relation of an instance's model nor the
            ``to_attribute`` of a ``Prefetch`` given with it.
    """
    from hare.query.relation_loading.prefetching.prefetch import Prefetch
    from hare.query.relation_loading.prefetching.prefetch_plan import PrefetchPlan
    from hare.query.relation_loading.prefetching.prefetch_request import PrefetchRequest
    from hare.query.relation_loading.prefetching.prefetch_result import PrefetchResult
    from hare.query.relation_loading.prefetching.prefetcher import Prefetcher

    instances_by_model: defaultdict[type[Model], list[Model]] = defaultdict(list)
    for instance in objs:
        instances_by_model[type(instance)].append(instance)
    if not instances_by_model:
        return
    first_instance = next(iter(instances_by_model.values()))[0]
    context = first_instance._context
    graph = first_instance._binding.graph
    if any(
        instance._context is not context or instance._binding.graph is not graph
        for instances in instances_by_model.values()
        for instance in instances
    ):
        raise QueryError("Prefetch requires instances from one model graph and application context")
    with QueryExecution(context):
        connection = InstanceConnections.get_connection_for_instance(first_instance, using=using)
        if len(instances_by_model) == 1:
            await Prefetcher(first_instance._binding, connection, context).load(
                instances_by_model[type(first_instance)], *lookups, db_explicitly_chosen=using is not None
            )
            return
        # Every model's lookups are checked before any query - a lookup another model lacks loads nothing.
        compatible_instances: dict[tuple[Any, ...], tuple[ModelBinding, list[Model], PrefetchRequest]] = {}
        for model in instances_by_model:
            prefetch_map: dict[str, set[str | Prefetch]] = {}
            prefetch_queries: dict[str, list[tuple[str | None, QuerySet[Any]]]] = {}
            meta = graph.get_binding(model)
            Prefetch.add_lookups(meta, prefetch_map, prefetch_queries, lookups)
            forward_fields = meta.forward_key_relation_names
            signature = (
                meta.default_connection,
                model if ModelConnectionChoice.has_route(context, meta) else None,  # type: ignore[arg-type]
                *(
                    (
                        name,
                        name in forward_fields,
                        meta.fields_map[name].related_model if name in forward_fields else model,  # type: ignore[attr-defined]
                    )
                    for name in sorted(prefetch_map.keys() | prefetch_queries.keys())
                    # Dependent to_attribute paths belong to their explicit source
                    # prefetch, whose relation already contributes to this signature.
                    if name in meta.fetch_fields
                ),
            )
            if signature in compatible_instances:
                compatible_instances[signature][1].extend(instances_by_model[model])
            else:
                compatible_instances[signature] = (
                    meta,
                    instances_by_model[model],
                    PrefetchRequest(prefetch_map, prefetch_queries, db_explicitly_chosen=using is not None),
                )
        # Forward relations to the same model share a query; reverse relations keep their source model.
        plans = []
        for binding, model_instances, request in compatible_instances.values():
            plans.append(
                PrefetchPlan.prepare(
                    Prefetcher(binding, connection, context),
                    model_instances,
                    request,
                )
            )
        result = PrefetchResult()
        for plan in plans:
            result.extend(await plan.fetch())
        result.attach()
