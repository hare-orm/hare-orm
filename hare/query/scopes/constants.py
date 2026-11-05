from __future__ import annotations

#: The module of each exported name - imported on first use: the expressions the scopes build on
#: import the visibility module themselves.
EXPORTED_MODULES = {
    "RowScopeProvider": "hare.query.scopes.row_scope_provider",
    "RowVisibility": "hare.query.scopes.row_visibility",
    "RowScopes": "hare.query.scopes.row_scopes",
}
