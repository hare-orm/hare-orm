from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


@dataclasses.dataclass(frozen=True, slots=True)
class RelationDescription:
    """The relation a filter parameter goes through - the last one its path crosses.

    Attributes:
        path: The relations from the query's model to it, joined with ``__`` (``author``,
            ``author__profile``).
        model: The related model.
        key_fields: The fields of the related model's primary key - every field of a composite
            one.
        to_many: Whether a row has many related rows (a reverse FK or a many-to-many relation).
        compares_key: Whether the parameter's value is the related row's key (``author``,
            ``author__in``, ``tags__pk__in``) - a relation picker - rather than a value of one of
            its fields (``author__name``).
    """

    path: str
    model: type[Model]
    key_fields: tuple[str, ...]
    to_many: bool
    compares_key: bool
