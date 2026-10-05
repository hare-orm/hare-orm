from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.object_types.dictionary_object_type import DictionaryObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.schema_objects.dictionary import Dictionary


class AddDictionary(AddSchemaObject):
    """Creates a dictionary a model declares; going back drops it.

    Args:
        model_name: The model.
        dictionary: The dictionary.
    """

    object_type: ClassVar[type[DictionaryObjectType]] = DictionaryObjectType

    def __init__(self, model_name: str, dictionary: Dictionary) -> None:
        super().__init__(model_name)
        self.dictionary = dictionary

    @property
    def schema_object(self) -> Any:
        return self.dictionary
