"""Operations on the fields of a model."""

from __future__ import annotations

from hare.migrations.operations.fields.add_field import AddField
from hare.migrations.operations.fields.alter_field import AlterField
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.operations.fields.remove_field import RemoveField
from hare.migrations.operations.fields.rename_field import RenameField

__all__ = ["AddField", "AlterField", "FieldLike", "RemoveField", "RenameField"]
