from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


@dataclasses.dataclass(frozen=True, slots=True)
class OrderingInfo:
    """What one ``.order_by()`` name orders by.

    ``-author__name`` orders by ``Author.name`` descending across ``author``; ``author`` orders by
    the relation's own key column(s) with no join; ``pk`` of a composite primary key orders by
    every key field in key order.

    Attributes:
        name: The ordering name, with its ``-``.
        model: The model the name starts at.
        relations: The relations the name crosses, in order.
        fields: The fields ordered by - several for a composite key; None in place of an
            annotation's field, whose type is only known once the query runs.
        paths: The names the query orders by - the name itself, except that the model's own
            ``pk`` becomes its key field(s) (``("id", "version")`` for a composite key) and a
            forward relation its own key column(s) (``author`` becomes ``author_id``).
        transforms: The path read inside the field's value (a JSON key path, an array index,
            a range bound).
        descending: Whether the order is descending.
        crosses_to_many: Whether a relation the name crosses holds many rows for one row - the
            query then has a row per related row.
    """

    name: str
    model: type[Model]
    relations: tuple[Field[Any], ...]
    fields: tuple[Field[Any] | None, ...]
    paths: tuple[str, ...]
    transforms: tuple[str, ...]
    descending: bool
    crosses_to_many: bool
