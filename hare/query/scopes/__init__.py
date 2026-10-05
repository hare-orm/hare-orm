"""Which rows a query sees: the default scopes of a model (soft delete, tenant, a custom manager)
and the visibility a queryset asks for."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hare.classes.lazy_exports import LazyExports
from hare.query.scopes.constants import EXPORTED_MODULES

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.scopes.row_scopes import RowScopes
    from hare.query.scopes.row_visibility import RowVisibility

__all__ = [
    "RowVisibility",
    "RowScopes",
]


__getattr__ = LazyExports(__name__, EXPORTED_MODULES).get
