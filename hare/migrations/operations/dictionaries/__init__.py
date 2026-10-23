"""Operations on the dictionaries of a model."""

from __future__ import annotations

from hare.migrations.operations.dictionaries.add_dictionary import AddDictionary
from hare.migrations.operations.dictionaries.alter_dictionary import AlterDictionary
from hare.migrations.operations.dictionaries.remove_dictionary import RemoveDictionary
from hare.migrations.operations.dictionaries.rename_dictionary import RenameDictionary

__all__ = ["AddDictionary", "AlterDictionary", "RemoveDictionary", "RenameDictionary"]
