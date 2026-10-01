"""Which rows a query sees: the default scopes of a model (soft delete, tenant, a custom manager)
and the visibility a queryset asks for."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.scopes.row_scopes import RowScopes
    from hare.query.scopes.row_visibility import RowVisibility

__all__ = [
    "RowVisibility",
    "RowScopes",
]

#: The module of each exported name - imported on first use: the expressions the scopes build on
#: import the visibility module themselves.
EXPORTED_MODULES = {
    "RowVisibility": "hare.query.scopes.row_visibility",
    "RowScopes": "hare.query.scopes.row_scopes",
}


def __getattr__(name: str) -> Any:
    module_name = EXPORTED_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)
