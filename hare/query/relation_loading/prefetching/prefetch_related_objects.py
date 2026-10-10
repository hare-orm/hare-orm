from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from hare.core.connections.connections import Connections
from hare.models.instances.instance_connections import InstanceConnections

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.relation_loading.prefetching.prefetch import Prefetch


async def prefetch_related_objects(
    objs: Iterable[Model], *lookups: str | Prefetch, using: str | DatabaseClient | None = None
) -> None:
    """Loads relations of objs already in hand, as ``prefetch_related()`` does for the rows
    of a queryset - one query per relation for all of them.

    .. code-block:: python3

        await prefetch_related_objects(users, "emails", Prefetch("posts", Post.objects.filter(published=True)))

    Args:
        objs: Instances of one model.
        lookups: Relation paths (``"posts__comments"``), or ``Prefetch(...)`` for a custom
            queryset or ``to_attribute``.
        using: Connection to read through instead of the one the first instance came from.

    Raises:
        FieldError: A lookup starts with neither a relation of the model nor the ``to_attribute`` of a
            ``Prefetch`` given with it.
    """
    from hare.query.relation_loading.prefetching.prefetcher import Prefetcher

    instance_list = list(objs)
    if not instance_list:
        return
    first_instance = instance_list[0]
    connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(first_instance)
    await Prefetcher(type(first_instance), connection).load(
        instance_list, *lookups, db_explicitly_chosen=using is not None
    )
