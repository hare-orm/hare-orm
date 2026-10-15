from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.alter_schema_object import AlterSchemaObject
from hare.migrations.operations.schema_objects.object_types.dictionary_object_type import DictionaryObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.schema_objects.dictionary import Dictionary


class AlterDictionary(AlterSchemaObject):
    """Replaces a dictionary of a model by its new version of the same name; going back restores
    the old one.

    Args:
        model_name: The model.
        dictionary: The new version.
    """

    object_type: ClassVar[type[DictionaryObjectType]] = DictionaryObjectType

    def __init__(self, model_name: str, dictionary: Dictionary) -> None:
        super().__init__(model_name)
        self.dictionary = dictionary

    @property
    def schema_object(self) -> Any:
        return self.dictionary
