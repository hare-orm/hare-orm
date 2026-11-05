from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.enums import FunctionVolatility, GrantTarget
from hare.ddl.schema_objects.database_function import DatabaseFunction
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.schema_objects.database_functions import DatabaseFunctions
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_DEFAULT_FUNCTION_LANGUAGE,
    POSTGRESQL_FUNCTION_CREATE_TEMPLATE,
    POSTGRESQL_FUNCTION_DROP_TEMPLATE,
    POSTGRESQL_FUNCTION_RENAME_TEMPLATE,
    POSTGRESQL_FUNCTION_VOLATILITY_KEYWORDS,
)
from hare.dialects.postgresql.schema.schema_objects.postgresql_function_arguments import PostgresqlFunctionArguments
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlDatabaseFunctions(DatabaseFunctions):
    """DatabaseFunctions as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_function_create_sqls(
        self, model: type[Model], function: DatabaseFunction, safe: bool = False
    ) -> list[str]:
        """The ``CREATE FUNCTION``, quoting its body with the dialect's string literal renderer."""
        return [
            POSTGRESQL_FUNCTION_CREATE_TEMPLATE.format(
                or_replace="OR REPLACE " if safe else "",
                function=self.editor.qualify_object_name(model, function.name),
                arguments=", ".join(function.arguments),
                returns=function.returns,
                language=function.language or POSTGRESQL_DEFAULT_FUNCTION_LANGUAGE,
                volatility=POSTGRESQL_FUNCTION_VOLATILITY_KEYWORDS[FunctionVolatility(function.volatility)],
                security=" SECURITY DEFINER" if function.security_definer else "",
                body=self.editor.client.dialect.literals.get_string_literal_sql(function.body.sql),
            )
        ]

    async def drop_database_function(self, model: type[Model], function: DatabaseFunction) -> None:
        await self.editor.run_sql(
            POSTGRESQL_FUNCTION_DROP_TEMPLATE.format(
                function=self.editor.qualify_object_name(model, function.name),
                arguments=PostgresqlFunctionArguments.get_identity_sql(function.arguments),
            )
        )

    async def alter_database_function(
        self, model: type[Model], old_function: DatabaseFunction, new_function: DatabaseFunction
    ) -> None:
        """Replaces the function in place (``CREATE OR REPLACE``, its grants kept) when its arguments
        and result type stay; otherwise drops it, creates the new one and grants on it again."""
        if old_function.arguments == new_function.arguments and old_function.returns == new_function.returns:
            await self.editor.run_sqls(self.get_function_create_sqls(model, new_function, safe=True))
            return
        statements = self.get_function_create_sqls(model, new_function)
        statements += self.editor.grants.get_grant_again_sqls(model, GrantTarget.FUNCTION, new_function.name)
        await self.drop_database_function(model, old_function)
        await self.editor.run_sqls(statements)

    async def rename_database_function(
        self, model: type[Model], old_function: DatabaseFunction, new_function: DatabaseFunction
    ) -> None:
        if old_function.name == new_function.name:
            return
        await self.editor.run_sql(
            POSTGRESQL_FUNCTION_RENAME_TEMPLATE.format(
                function=self.editor.qualify_object_name(model, old_function.name),
                arguments=PostgresqlFunctionArguments.get_identity_sql(old_function.arguments),
                new_name=self.editor.quote(new_function.name),
            )
        )

    def get_schema_move_plan(
        self, model: type[Model], function: DatabaseFunction, old_schema: str | None, new_schema: str | None
    ) -> SchemaChangePlan:
        qualified_function = self.editor.qualify_table_name(function.name, old_schema)
        return self.editor.schemas.get_object_schema_move_plan(
            "FUNCTION",
            f"{qualified_function}({PostgresqlFunctionArguments.get_identity_sql(function.arguments)})",
            new_schema,
        )
