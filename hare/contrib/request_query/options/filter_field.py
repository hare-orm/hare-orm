from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from hare.contrib.request_query.constants import (
    EXACT_LOOKUP_NAME,
    LOOKUP_SEPARATOR,
)
from hare.exceptions import ConfigurationError
from hare.query.enums import Lookup

if TYPE_CHECKING:  # pragma: nocoverage
    pass


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

    Raises:
        ConfigurationError: The path or the parameter isn't an identifier, the lookups aren't a
            non-empty tuple of distinct lookup names, or the description isn't text.
    """

    path: str
    lookups: tuple[Lookup | str, ...] = (Lookup.EXACT,)
    parameter: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.isidentifier():
            raise ConfigurationError(f"FilterField path must be a field path, got {self.path!r}")
        if (
            not isinstance(self.lookups, tuple)
            or not self.lookups
            or not all(isinstance(lookup, str) for lookup in self.lookups)
        ):
            raise ConfigurationError(
                f"FilterField({self.path!r}) lookups must be a non-empty tuple of lookups, got {self.lookups!r}"
            )
        if len(set(self.get_filter_keys())) != len(self.lookups):
            raise ConfigurationError(f"FilterField({self.path!r}) names a lookup twice: {self.lookups!r}")
        if self.parameter is not None and (not isinstance(self.parameter, str) or not self.parameter.isidentifier()):
            raise ConfigurationError(
                f"FilterField({self.path!r}) parameter must be an identifier or None, got {self.parameter!r}"
            )
        if self.description is not None and not isinstance(self.description, str):
            raise ConfigurationError(
                f"FilterField({self.path!r}) description must be text or None, got {self.description!r}"
            )

    def get_filter_keys(self) -> dict[str, str]:
        """The parameters of the field.

        Returns:
            Each parameter's ``.filter()`` key, by parameter.
        """
        name = self.parameter or self.path
        filter_keys: dict[str, str] = {}
        for lookup in self.lookups:
            if lookup in (Lookup.EXACT, EXACT_LOOKUP_NAME):
                filter_keys[name] = self.path
            else:
                filter_keys[f"{name}{LOOKUP_SEPARATOR}{lookup}"] = f"{self.path}{LOOKUP_SEPARATOR}{lookup}"
        return filter_keys
