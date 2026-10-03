from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions import Q


class Select:
    """
    select_related() relation container. One would directly use this when wanting to attach an
    extra condition to that relation's JOIN itself - analogous to how ``Prefetch`` attaches a
    custom queryset to a ``prefetch_related()`` relation.

    Args:
        relation: Related field name (dotted for a nested path, e.g. ``"a__b"``).
        extra_condition: ``Q`` condition ANDed into this relation's JOIN itself (not the
            query's WHERE) - a LEFT JOIN, so a row whose related object doesn't satisfy the
            condition still comes back, with that attribute set to ``None`` rather than being
            filtered out of the result entirely.
    """

    __slots__ = ("relation", "extra_condition")

    def __init__(self, relation: str, extra_condition: Q | None = None) -> None:
        self.relation = relation
        self.extra_condition = extra_condition
