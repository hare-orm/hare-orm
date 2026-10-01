"""Operations on the indexes of a model."""

from hare.migrations.operations.indexes.add_index import AddIndex
from hare.migrations.operations.indexes.declarations import RenameIndex
from hare.migrations.operations.indexes.remove_index import RemoveIndex

__all__ = [
    "AddIndex",
    "RemoveIndex",
    "RenameIndex",
]
