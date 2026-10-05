"""Operations on database extensions and collations."""

from __future__ import annotations

from hare.migrations.operations.extensions.create_collation import CreateCollation
from hare.migrations.operations.extensions.create_extension import CreateExtension
from hare.migrations.operations.extensions.remove_collation import RemoveCollation
from hare.migrations.operations.extensions.remove_extension import RemoveExtension

__all__ = [
    "CreateCollation",
    "CreateExtension",
    "RemoveCollation",
    "RemoveExtension",
]
