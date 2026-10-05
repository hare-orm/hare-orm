from __future__ import annotations

#: The module of each exported name - imported on first use: the expressions the scopes build on
#: import the visibility module themselves.
EXPORTED_MODULES = {
    "RowVisibility": "hare.query.scopes.row_visibility",
    "RowScopes": "hare.query.scopes.row_scopes",
}

#: The attribute of ``RowVisibility`` the default visibility resolved for the last tenant is kept in.
RESOLVED_DEFAULT_ATTRIBUTE = "resolved_default"

#: The attribute of a model's ``RowScopes`` the last description of its filters is kept in.
DESCRIBED_FILTERS_ATTRIBUTE = "last_described_filters"
