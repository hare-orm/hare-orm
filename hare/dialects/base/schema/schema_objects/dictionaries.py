from __future__ import annotations

from hare.ddl.schema_objects.dictionary import Dictionary
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.models import Model


class Dictionaries(SchemaEditorPart):
    """Dictionaries a model declares: created, dropped, replaced, renamed and loaded again."""

    __slots__ = ()

    def get_dictionary_create_sqls(self, model: type[Model], dictionary: Dictionary, safe: bool = False) -> list[str]:
        """The statements creating a dictionary of a model.

        Args:
            model: The model declaring it.
            dictionary: The dictionary.
            safe: Create it only when it doesn't exist yet.

        Raises:
            UnSupportedError: The dialect has no dictionaries.
        """
        raise self.get_unsupported_error("Dictionaries")

    async def create_dictionary(self, model: type[Model], dictionary: Dictionary) -> None:
        """Creates a dictionary of a model."""
        await self.editor.run_sqls(self.get_dictionary_create_sqls(model, dictionary))

    async def drop_dictionary(self, model: type[Model], dictionary: Dictionary) -> None:
        """Drops a dictionary of a model.

        Raises:
            UnSupportedError: The dialect has no dictionaries.
        """
        raise self.get_unsupported_error("Dictionaries")

    async def drop_model_dictionaries(self, model: type[Model]) -> None:
        """Drops the dictionaries a model declares, before its table is dropped - nothing by default:
        a dialect without dictionaries has none.

        Args:
            model: The model.
        """

    async def alter_dictionary(
        self, model: type[Model], old_dictionary: Dictionary, new_dictionary: Dictionary
    ) -> None:
        """Replaces a dictionary of a model by its new version of the same name.

        Args:
            model: The model.
            old_dictionary: The dictionary as it is.
            new_dictionary: The dictionary as it becomes.
        """
        await self.drop_dictionary(model, old_dictionary)
        await self.create_dictionary(model, new_dictionary)

    async def rename_dictionary(
        self, model: type[Model], old_dictionary: Dictionary, new_dictionary: Dictionary
    ) -> None:
        """Renames a dictionary of a model.

        Raises:
            UnSupportedError: The dialect has no dictionaries.
        """
        raise self.get_unsupported_error("Dictionaries")

    async def reload_dictionary(self, model: type[Model], dictionary: Dictionary) -> None:
        """Loads a dictionary of a model from its source again.

        Raises:
            UnSupportedError: The dialect has no dictionaries.
        """
        raise self.get_unsupported_error("Dictionaries")
