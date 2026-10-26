from __future__ import annotations

import dataclasses

from hare.contrib.request_query.constants import LOOKUP_SEPARATOR
from hare.contrib.request_query.options.constants import EXACT_LOOKUP_NAME
from hare.query.enums import Lookup


@dataclasses.dataclass(frozen=True, slots=True)
class FilterField:
    """A field a request may filter by, and the lookups it may filter it by - one parameter per
    lookup, typed and described by the ORM (``Meta.filters``)::

        filters = (
            FilterField("status", lookups=(Lookup.EXACT, Lookup.IN)),
            FilterField("published_at", lookups=(Lookup.GTE, Lookup.LTE)),
            FilterField("author__profile__city", lookups=(Lookup.EXACT, Lookup.IN), parameter="city"),
        )

    makes the parameters ``status``, ``status__in``, ``published_at__gte``, ``published_at__lte``,
    ``city`` and ``city__in``.

    Args:
        path: The field - a field, a relation, a path through relations, an annotation of
            ``Meta.queryset``.
        lookups: The lookups - ``Lookup.EXACT`` (or ``"exact"``) is a parameter named as the field
            itself, any other adds ``__<lookup>``; a lookup registered with ``register_lookup()``
            by its name.
        parameter: The name the parameters start with in place of the path - so the API doesn't
            show the model's structure; None for the path.
        description: The parameters' description in the API schema in place of the field's; None
            for the field's.
    """

    path: str
    lookups: tuple[Lookup | str, ...] = (Lookup.EXACT,)
    parameter: str | None = None
    description: str | None = None

    def get_filter_keys(self) -> dict[str, str]:
        """The parameters of the field.

        Returns:
            Each parameter's ``.filter()`` key, by parameter.
        """
        name = self.parameter or self.path
        filter_keys: dict[str, str] = {}
        for lookup in self.lookups:
            if lookup in {Lookup.EXACT, EXACT_LOOKUP_NAME}:
                filter_keys[name] = self.path
            else:
                filter_keys[f"{name}{LOOKUP_SEPARATOR}{lookup}"] = f"{self.path}{LOOKUP_SEPARATOR}{lookup}"
        return filter_keys
