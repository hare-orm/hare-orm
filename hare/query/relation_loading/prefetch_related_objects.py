from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from hare.core.connections import Connections

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.relation_loading.prefetch import Prefetch


async def prefetch_related_objects(
    instances: Iterable[Model], *lookups: str | Prefetch, using: str | DatabaseClient | None = None
) -> None:
    """Loads relations of instances already in hand, as ``prefetch_related()`` does for the rows
    of a queryset - one query per relation for all of them.

    .. code-block:: python3

        await prefetch_related_objects(users, "emails", Prefetch("posts", Post.objects.filter(published=True)))

    Args:
        instances: Instances of one model.
        lookups: Relation paths (``"posts__comments"``), or ``Prefetch(...)`` for a custom
            queryset or ``to_attr``.
        using: Connection to read through instead of the one the first instance came from.

    Raises:
        FieldError: A lookup starts with neither a relation of the model nor the ``to_attr`` of a
            ``Prefetch`` given with it.
    """
    from hare.query.relation_loading.prefetcher import Prefetcher

    instance_list = list(instances)
    if not instance_list:
        return
    first_instance = instance_list[0]
    db = Connections.get_client(using) or first_instance._get_connection_for_instance()
    await Prefetcher(type(first_instance), db).load(instance_list, *lookups, db_explicitly_chosen=using is not None)
