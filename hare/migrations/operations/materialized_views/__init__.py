"""Operations on the materialized views of a model."""

from __future__ import annotations

from hare.migrations.operations.materialized_views.add_materialized_view import AddMaterializedView
from hare.migrations.operations.materialized_views.alter_materialized_view import AlterMaterializedView
from hare.migrations.operations.materialized_views.refresh_materialized_view import RefreshMaterializedView
from hare.migrations.operations.materialized_views.remove_materialized_view import RemoveMaterializedView
from hare.migrations.operations.materialized_views.rename_materialized_view import RenameMaterializedView

__all__ = [
    "AddMaterializedView",
    "AlterMaterializedView",
    "RefreshMaterializedView",
    "RemoveMaterializedView",
    "RenameMaterializedView",
]
