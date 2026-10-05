from __future__ import annotations

from typing import Any, cast

from hare.query.scopes.row_scope import RowScope
from hare.query.scopes.row_visibility import RowVisibility


class SoftDeleteScope(RowScope):
    """``Meta.soft_delete_field``: soft-deleted rows are hidden; ``.include_deleted()`` shows them."""

    __slots__ = ()

    def get_filter(self, visibility: RowVisibility) -> tuple[str, Any] | None:
        if visibility.include_deleted:
            return None
        return cast("str", self.model._meta.soft_delete_field), None
