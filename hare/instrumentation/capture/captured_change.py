from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.instrumentation.enums import RowOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


@dataclasses.dataclass(frozen=True, slots=True)
class CapturedChange:
    """One row a write changed, as ``Meta.change_capture`` captures it - in the write's transaction.

    Attributes:
        model: The row's model.
        operation: Inserted, updated or deleted.
        pk: The row's primary key - a tuple for a composite one.
        changed: The fields an update set, None when not known or for an insert or delete.
        before: The captured fields as they were before the write, by name - None when the payload
            holds no row before it, and for an insert.
        after: The captured fields as the write left them - None when the payload holds no row after
            it, and for a delete.
        occurred_at: When the write ran.
        tenant: The row's tenant (``Meta.tenant_field``), None for a model without tenants.
    """

    model: type[Model]
    operation: RowOperation
    pk: Any
    changed: tuple[str, ...] | None
    before: Mapping[str, Any] | None
    after: Mapping[str, Any] | None
    occurred_at: datetime.datetime
    tenant: Any = None
