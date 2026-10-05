"""Operations on the views of a model."""

from __future__ import annotations

from hare.migrations.operations.views.add_view import AddView
from hare.migrations.operations.views.alter_view import AlterView
from hare.migrations.operations.views.remove_view import RemoveView
from hare.migrations.operations.views.rename_view import RenameView

__all__ = ["AddView", "AlterView", "RemoveView", "RenameView"]
