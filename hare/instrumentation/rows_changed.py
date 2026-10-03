from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, ClassVar

from hare.instrumentation.enums import RowOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


@dataclasses.dataclass(frozen=True, slots=True)
class RowsChanged:
    """Rows of one model a committed write changed - see ``ChangeEvents`` for which writes report.

    Attributes:
        model: The model.
        operation: Inserted, updated or deleted.
        pks: The primary keys of the rows; None when the write names no rows - a
            ``QuerySet.update()``/``delete()``, the rows ``on_delete`` reaches, a model without
            a primary key.
        fields: The fields the write set; None when it isn't known - an insert, a ``save()``
            without ``update_fields``.
        connection_name: The connection the write ran on.
    """

    #: Observers of this event can be narrowed to models - and their subclasses.
    observed_by_model: ClassVar[bool] = True

    model: type[Model]
    operation: RowOperation
    pks: tuple[Any, ...] | None
    fields: tuple[str, ...] | None
    connection_name: str
