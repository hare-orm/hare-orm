from __future__ import annotations

import secrets
from types import TracebackType
from typing import TYPE_CHECKING, Self

from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.schema.constants import SCHEMA_CLEANUP_FAILED
from hare.dialects.base.schema.shadow.shadow_database import ShadowDatabase
from hare.dialects.postgresql.client.constants import POSTGRESQL_SET_TRANSACTION_SEARCH_PATH_SQL
from hare.dialects.postgresql.connection.search_path import SearchPath
from hare.dialects.postgresql.constants import (
    POSTGRESQL_SEARCH_PATH_SQL,
    POSTGRESQL_SHADOW_SCHEMA_CREATE_TEMPLATE,
    POSTGRESQL_SHADOW_SCHEMA_PREFIX,
)
from hare.dialects.postgresql.introspection.constants import POSTGRESQL_CATALOG_REFERENCE_CASTS
from hare.inspectdb.introspection.catalog_identities import CatalogIdentities
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
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
                    self.real_schema_by_shadow_schema[self.get_shadow_schema_name(schema)] = schema  # type: ignore[index]
            quoted_prefix = self.connection.dialect.literals.quote_identifier(self.prefix)
            await self.client.execute_script(POSTGRESQL_SHADOW_SCHEMA_CREATE_TEMPLATE.format(schema=quoted_prefix))
            search_path = SearchPath.parse(rows[0]["search_path"]).prepended(self.prefix)
            await self.client.execute_dicts(POSTGRESQL_SET_TRANSACTION_SEARCH_PATH_SQL, [search_path.to_setting()])
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
        errors: list[BaseException] = []
        if self.client is not None and self.transaction is not None:
            cause = exception
            try:
                await self.client.rollback()  # type: ignore[attr-defined]
            except BaseException as error:
                errors.append(error)
                cause = error
            try:
                # The context token belongs to this task. A failed rollback must still
                # exit the context, and must never turn that exit into a commit attempt.
                await self.transaction.__aexit__(
                    type(cause) if cause is not None else None,
                    cause,
                    cause.__traceback__ if cause is not None else None,
                )
            except BaseException as error:
                errors.append(error)
        if self.independent_client is not None:
            try:
                await ResourceCompletion.wait(self.independent_client.close())
            except BaseException as error:
                errors.append(error)
        if errors:
            if exception is not None:
                errors.insert(0, exception)
            if len(errors) == 1:
                raise errors[0]
            raise BaseExceptionGroup(SCHEMA_CLEANUP_FAILED, errors)

    def get_shadow_schema_name(self, schema: str | None) -> str | None:
        if schema is None or schema == self.default_schema:
            return self.prefix
        return Identifiers.get_within_limit(f"{self.prefix}_{schema}")

    def get_catalog_identities(self) -> CatalogIdentities:
        return CatalogIdentities(self.real_schema_by_shadow_schema, POSTGRESQL_CATALOG_REFERENCE_CASTS)
