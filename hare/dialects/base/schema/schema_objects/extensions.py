from __future__ import annotations

from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError


class Extensions(SchemaEditorPart):
    """Database extensions and collations the models need: created and dropped."""

    __slots__ = ()

    def get_extension_create_sql(self, extension: str) -> str:
        """Returns the statement installing a database extension unless it is installed, or an
        empty string where the database has no extensions.

        Args:
            extension: The extension's name.

        Returns:
            The statement.
        """
        return ""

    async def create_extension(self, extension_name: str) -> None:
        """Installs a database extension, unless it is installed.

        Args:
            extension_name: The extension's name.

        Raises:
            UnSupportedError: The dialect has no extensions.
        """
        raise self.get_unsupported_error("Database extensions")

    async def drop_extension(self, extension_name: str) -> None:
        """Removes a database extension, if it is installed.

        Args:
            extension_name: The extension's name.

        Raises:
            UnSupportedError: The dialect has no extensions.
        """
        raise self.get_unsupported_error("Database extensions")

    async def create_collation(self, name: str, locale: str, provider: str, deterministic: bool) -> None:
        """Creates a collation.

        Args:
            name: The collation's name.
            locale: Its locale, e.g. ``"und-u-ks-level2"``.
            provider: ``"libc"`` or ``"icu"``.
            deterministic: False for a collation equal strings can differ under.

        Raises:
            UnSupportedError: The dialect has no collation DDL.
        """
        raise UnSupportedError(f"Creating a collation is not supported on {self.editor.client.dialect}")

    async def drop_collation(self, name: str) -> None:
        """Drops a collation.

        Args:
            name: The collation's name.

        Raises:
            UnSupportedError: The dialect has no collation DDL.
        """
        raise UnSupportedError(f"Dropping a collation is not supported on {self.editor.client.dialect}")
