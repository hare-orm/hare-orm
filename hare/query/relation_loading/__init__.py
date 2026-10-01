"""Loading the relations of instances - ``select_related()`` joins, ``prefetch_related()`` queries
and ``prefetch_related_objects()`` for instances already in hand."""

from hare.query.relation_loading.prefetch import Prefetch
from hare.query.relation_loading.prefetch_related_objects import prefetch_related_objects
from hare.query.relation_loading.select import Select

__all__ = [
    "Prefetch",
    "Select",
    "prefetch_related_objects",
]
