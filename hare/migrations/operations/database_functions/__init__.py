"""Operations on the functions a model stores in the database."""

from __future__ import annotations

from hare.migrations.operations.database_functions.add_function import AddFunction
from hare.migrations.operations.database_functions.alter_function import AlterFunction
from hare.migrations.operations.database_functions.remove_function import RemoveFunction
from hare.migrations.operations.database_functions.rename_function import RenameFunction

__all__ = ["AddFunction", "AlterFunction", "RemoveFunction", "RenameFunction"]
