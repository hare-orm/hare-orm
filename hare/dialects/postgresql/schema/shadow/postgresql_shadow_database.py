from __future__ import annotations

import secrets
from types import TracebackType
from typing import TYPE_CHECKING, Self, cast

from hare.dialects.base.schema.shadow.shadow_database import ShadowDatabase
from hare.dialects.postgresql.constants import (
    POSTGRESQL_SEARCH_PATH_SQL,
    POSTGRESQL_SHADOW_SCHEMA_CREATE_TEMPLATE,
    POSTGRESQL_SHADOW_SCHEMA_PREFIX,
    POSTGRESQL_SHADOW_SEARCH_PATH_TEMPLATE,
)
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext


class PostgresqlShadowDatabase(ShadowDatabase):
    """ShadowDatabase as PostgreSQL makes it: a schema ``hare_shadow_<hex>`` - and one more for each schema of
    the models - in a transaction of a connection of its own, searched first, rolled back on leaving: none of
    it is ever committed."""

    independent_client: DatabaseClient | None = None
    transaction: TransactionContext | None = None

    async def __aenter__(self) -> Self:
        self.prefix = f"{POSTGRESQL_SHADOW_SCHEMA_PREFIX}{secrets.token_hex(4)}"
        # The search path of the compared connection - the shadow's own connection may have another.
        rows = await self.connection.execute_dicts(POSTGRESQL_SEARCH_PATH_SQL)
        self.independent_client = self.connection.create_independent_client()
        try:
            self.transaction = self.independent_client._in_transaction()
            self.client = await self.transaction.__aenter__()
            self.default_schema: str = rows[0]["default_schema"]
            self.real_schema_by_shadow_schema = {self.prefix: self.default_schema}
            for schema in self.schemas:
                if schema != self.default_schema:
                    self.real_schema_by_shadow_schema[cast("str", self.get_shadow_schema_name(schema))] = schema
            quoted_prefix = self.connection.dialect.literals.quote_identifier(self.prefix)
            await self.client.execute_script(POSTGRESQL_SHADOW_SCHEMA_CREATE_TEMPLATE.format(schema=quoted_prefix))
            await self.client.execute_script(
                POSTGRESQL_SHADOW_SEARCH_PATH_TEMPLATE.format(schema=quoted_prefix, search_path=rows[0]["search_path"])
            )
        except BaseException as error:
            # No __aexit__() follows a failed __aenter__() - the transaction and the connection end here.
            await self.__aexit__(type(error), error, error.__traceback__)
            raise
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self.client is not None and self.transaction is not None:
                # Rolled back - the transaction then ends with nothing to commit.
                await cast("TransactionClient", self.client).rollback()
                await self.transaction.__aexit__(None, None, None)
        finally:
            if self.independent_client is not None:
                await self.independent_client.close()

    def get_shadow_schema_name(self, schema: str | None) -> str | None:
        if schema is None or schema == self.default_schema:
            return self.prefix
        return Identifiers.get_within_limit(f"{self.prefix}_{schema}")

    def replace_shadow_names(self, text: str) -> str:
        # The longest first - the shadow of the default schema begins each of the others.
        for shadow_schema, schema in sorted(self.real_schema_by_shadow_schema.items(), key=lambda item: -len(item[0])):
            text = text.replace(shadow_schema, schema)
        return text
