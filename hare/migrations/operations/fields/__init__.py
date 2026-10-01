"""Operations on the fields of a model."""

from hare.migrations.operations.fields.add_field import AddField
from hare.migrations.operations.fields.alter_field import AlterField
from hare.migrations.operations.fields.field_add_remove_operation import FieldAddRemoveOperation
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.operations.fields.remove_field import RemoveField
from hare.migrations.operations.fields.rename_field import RenameField

__all__ = [
    "AddField",
    "AlterField",
    "FieldAddRemoveOperation",
    "FieldLike",
    "RemoveField",
    "RenameField",
]
