from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.object_types.dictionary_object_type import DictionaryObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject


class RemoveDictionary(RemoveSchemaObject):
    """Drops a dictionary of a model; going back creates it again from the state.

    Args:
        model_name: The model.
        name: The dictionary's name.
    """

    object_type: ClassVar[type[DictionaryObjectType]] = DictionaryObjectType

    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)
