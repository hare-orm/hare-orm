from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from hare.core.connections.connections import Connections
from hare.fields.relations.fields.relational_field import RelationalField
from hare.models.instances.instance_connections import InstanceConnections

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
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
    from hare.query.relation_loading.prefetching.prefetcher import Prefetcher

    instances_by_model: dict[type[Model], list[Model]] = {}
    for instance in objs:
        instances_by_model.setdefault(type(instance), []).append(instance)
    if not instances_by_model:
        return
    first_instance = next(iter(instances_by_model.values()))[0]
    connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(first_instance)
    if len(instances_by_model) == 1:
        await Prefetcher(type(first_instance), connection).load(
            instances_by_model[type(first_instance)], *lookups, db_explicitly_chosen=using is not None
        )
        return
    # Every model's lookups are checked before any query - a lookup another model lacks loads nothing.
    compatible_instances: dict[tuple[Any, ...], tuple[type[Model], list[Model]]] = {}
    for model in instances_by_model:
        prefetch_map: dict[str, set[str | Prefetch]] = {}
        prefetch_queries: dict[str, list[tuple[str | None, QuerySet[Any]]]] = {}
        Prefetch.add_lookups(model, prefetch_map, prefetch_queries, lookups)
        meta = model._meta
        forward_fields = meta.foreign_key_fields | meta.one_to_one_fields
        signature = (
            meta.default_connection,
            model if InstanceConnections.is_routed(model) else None,
            *(
                (
                    name,
                    name in forward_fields,
                    cast("RelationalField[Any]", meta.fields_map[name]).related_model
                    if name in forward_fields
                    else model,
                )
                for name in sorted(prefetch_map.keys() | prefetch_queries.keys())
            ),
        )
        if signature in compatible_instances:
            compatible_instances[signature][1].extend(instances_by_model[model])
        else:
            compatible_instances[signature] = (model, instances_by_model[model])
    # Forward relations to the same model share a query; reverse relations keep their source model.
    for model, model_instances in compatible_instances.values():
        await Prefetcher(model, connection).load(model_instances, *lookups, db_explicitly_chosen=using is not None)
