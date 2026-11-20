from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding

from hare.ddl.schema_objects.dictionary import Dictionary
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart


class Dictionaries(SchemaEditorPart):
    """Dictionaries a model declares: created, dropped, replaced, renamed and loaded again."""

    __slots__ = ()

    def get_dictionary_create_sqls(
        self, binding: ModelBinding, dictionary: Dictionary, safe: bool = False
    ) -> list[str]:
        """The statements creating a dictionary of a model.

        Args:
            binding: The model declaring it.
            dictionary: The dictionary.
            safe: Create it only when it doesn't exist yet.

        Raises:
            UnSupportedError: The dialect has no dictionaries.
        """
        raise self.get_unsupported_error("Dictionaries")

    async def create_dictionary(self, binding: ModelBinding, dictionary: Dictionary) -> None:
        """Creates a dictionary of a model."""
        await self.editor.run_sqls(self.get_dictionary_create_sqls(binding, dictionary))

    async def drop_dictionary(self, binding: ModelBinding, dictionary: Dictionary) -> None:
        """Drops a dictionary of a model.

        Raises:
            UnSupportedError: The dialect has no dictionaries.
        """
        raise self.get_unsupported_error("Dictionaries")

    def get_model_drop_sqls(self, binding: ModelBinding) -> list[str]:
        """Dependent-object removal statements; empty for a dialect without these objects."""
        return []

    async def alter_dictionary(
        self, binding: ModelBinding, old_dictionary: Dictionary, new_dictionary: Dictionary
    ) -> None:
        """Replaces a dictionary of a model by its new version of the same name.

        Args:
            binding: The model.
            old_dictionary: The dictionary as it is.
            new_dictionary: The dictionary as it becomes.
        """
        await self.drop_dictionary(binding, old_dictionary)
        await self.create_dictionary(binding, new_dictionary)

    async def rename_dictionary(
        self, binding: ModelBinding, old_dictionary: Dictionary, new_dictionary: Dictionary
    ) -> None:
        """Renames a dictionary of a model.

        Raises:
            UnSupportedError: The dialect has no dictionaries.
        """
        raise self.get_unsupported_error("Dictionaries")

    async def reload_dictionary(self, binding: ModelBinding, dictionary: Dictionary) -> None:
        """Loads a dictionary of a model from its source again.

        Raises:
            UnSupportedError: The dialect has no dictionaries.
        """
        raise self.get_unsupported_error("Dictionaries")
