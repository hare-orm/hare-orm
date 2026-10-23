from __future__ import annotations

from collections.abc import Collection
from types import TracebackType
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient


class ShadowDatabase:
    """An empty place beside a connection's database - the schema the models declare is built in it, read
    back by the same introspector as the database itself and compared with it - removed on leaving,
    whatever happened. Each dialect with drift declares its own (``Dialect.shadow_database_class``).

    Args:
        connection: The connection whose database it stands beside.
        schemas: The schemas of the models - each gets a shadow of its own where the database has schemas.
    """

    def __init__(self, connection: DatabaseClient, schemas: Collection[str]) -> None:
        self.connection = connection
        self.schemas = tuple(schemas)
        #: The client the shadow is built and read on, set on entering.
        self.client: DatabaseClient | None = None

    async def __aenter__(self) -> Self:
        """Creates the shadow.

        Returns:
            The shadow, its ``client`` set.
        """
        raise NotImplementedError

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Removes the shadow."""
        raise NotImplementedError

    def get_shadow_schema_name(self, schema: str | None) -> str | None:
        """The shadow of a schema.

        Args:
            schema: A schema of the models - None for the connection's default one.

        Returns:
            The schema the shadow holds its objects in - the schema itself by default: the shadow is a
            database of its own.
        """
        return schema

    def get_shadow_table_options(self, options: TableOptions) -> TableOptions:
        """A model's table options as the shadow's table takes them - the same by default.

        Args:
            options: The options.

        Returns:
            The options.
        """
        return options

    def replace_shadow_names(self, text: str) -> str:
        """A text the shadow printed, its names of the shadow made the database's.

        Args:
            text: The text.

        Returns:
            The text - itself by default.
        """
        return text
