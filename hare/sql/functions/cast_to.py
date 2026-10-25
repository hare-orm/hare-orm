from __future__ import annotations

from typing import Any

from hare.sql.enums import CastType
from hare.sql.terms.functions.function import Function


class CastTo(Function):
    """A conversion to the type of ``target_field`` as its database converts - see ``Cast``. A naive
    timestamp (``is_aware=False``) converts as its wall clock."""

    requires_dialect_renderer = True

    def __init__(
        self,
        term: Any,
        target_field: Any,
        target: CastType,
        source: CastType,
        alias: str | None = None,
    ) -> None:
        """
        Args:
            term: The value.
            target_field: The field of the result, whose column type the value is cast to.
            target: The type converted to.
            source: The type of the value.
            alias: Optional alias for the term.
        """
        super().__init__("CAST", term, alias=alias)
        self.target_field = target_field
        self.target = target
        self.source = source
        self.is_aware = True
