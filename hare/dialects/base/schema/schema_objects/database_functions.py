from __future__ import annotations

from hare.ddl.schema_objects.database_function import DatabaseFunction
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.models import Model


class DatabaseFunctions(SchemaEditorPart):
    """Database functions a model declares: created, dropped, altered and renamed."""

    __slots__ = ()

    def get_function_create_sqls(
        self, model: type[Model], function: DatabaseFunction, safe: bool = False
    ) -> list[str]:
        """The statements creating a function of a model.

        Args:
            model: The model declaring it.
            function: The function.
            safe: Replace a function of the same name and arguments.

        Raises:
            UnSupportedError: The dialect stores no functions.
        """
        raise self.get_unsupported_error("Database functions")

    async def create_database_function(self, model: type[Model], function: DatabaseFunction) -> None:
        """Creates a function of a model."""
        await self.editor.run_sqls(self.get_function_create_sqls(model, function))

    async def drop_database_function(self, model: type[Model], function: DatabaseFunction) -> None:
        """Drops a function of a model.

        Raises:
            UnSupportedError: The dialect stores no functions.
        """
        raise self.get_unsupported_error("Database functions")

    async def alter_database_function(
        self, model: type[Model], old_function: DatabaseFunction, new_function: DatabaseFunction
    ) -> None:
        """Replaces a function of a model by its new version of the same name, with the grants on it.

        Raises:
            UnSupportedError: The dialect stores no functions.
        """
        raise self.get_unsupported_error("Database functions")

    async def rename_database_function(
        self, model: type[Model], old_function: DatabaseFunction, new_function: DatabaseFunction
    ) -> None:
        """Renames a function of a model.

        Raises:
            UnSupportedError: The dialect stores no functions.
        """
        raise self.get_unsupported_error("Database functions")
