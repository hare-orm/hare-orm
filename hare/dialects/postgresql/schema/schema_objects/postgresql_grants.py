from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.enums import GrantTarget, Privilege
from hare.ddl.schema_objects.database_function import DatabaseFunction
from hare.ddl.security.grant import Grant
from hare.dialects.base.schema.schema_objects.grants import Grants
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_GRANT_OBJECT_KEYWORDS,
    POSTGRESQL_GRANT_TEMPLATE,
    POSTGRESQL_PRIVILEGE_KEYWORDS,
    POSTGRESQL_REVOKE_TEMPLATE,
)
from hare.dialects.postgresql.schema.schema_objects.postgresql_function_arguments import PostgresqlFunctionArguments
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
    from hare.models.model_binding import ModelBinding


class PostgresqlGrants(Grants):
    """Grants as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_grant_sqls(self, binding: ModelBinding, grant: Grant) -> list[str]:
        return [
            POSTGRESQL_GRANT_TEMPLATE.format(
                privileges=self.get_privileges_sql(binding, grant),
                object_type=POSTGRESQL_GRANT_OBJECT_KEYWORDS[GrantTarget(grant.on)],
                object=self.get_grant_object_sql(binding, grant),
                roles=self.editor.get_roles_sql(grant.roles),
                grant_option=" WITH GRANT OPTION" if grant.with_grant_option else "",
            )
        ]

    def get_revoke_sqls(self, binding: ModelBinding, grant: Grant) -> list[str]:
        return [
            POSTGRESQL_REVOKE_TEMPLATE.format(
                privileges=self.get_privileges_sql(binding, grant),
                object_type=POSTGRESQL_GRANT_OBJECT_KEYWORDS[GrantTarget(grant.on)],
                object=self.get_grant_object_sql(binding, grant),
                roles=self.editor.get_roles_sql(grant.roles),
            )
        ]

    def get_grant_object_sql(self, binding: ModelBinding, grant: Grant) -> str:
        """The object a grant is on - a function with its argument types.

        Raises:
            ConfigurationError: The model declares no object of the grant's type and name.
        """
        model = binding._model
        if grant.on == GrantTarget.TABLE:
            return self.editor.get_model_table_sql(binding)
        declared_objects: dict[GrantTarget, tuple[Any, ...]] = {
            GrantTarget.VIEW: binding.views,
            GrantTarget.MATERIALIZED_VIEW: binding.materialized_views,
            GrantTarget.SEQUENCE: binding.sequences,
            GrantTarget.FUNCTION: binding.functions,
        }
        declared_object = next(
            (
                schema_object
                for schema_object in declared_objects[GrantTarget(grant.on)]
                if schema_object.name == grant.object_name
            ),
            None,
        )
        if declared_object is None:
            raise ConfigurationError(
                f"Grant of {grant.describe()}: {model.__name__} declares no {grant.on} named {grant.object_name!r}"
            )
        qualified_object = self.editor.qualify_object_name(binding, declared_object.name)
        if isinstance(declared_object, DatabaseFunction):
            return f"{qualified_object}({PostgresqlFunctionArguments.get_identity_sql(declared_object.arguments)})"
        return qualified_object

    def get_privileges_sql(self, binding: ModelBinding, grant: Grant) -> str:
        """A grant's privileges, each with its columns."""
        columns_sql = ""
        if grant.columns:
            columns = [
                self.editor.get_column_name(binding, field_name, "Grant.columns") for field_name in grant.columns
            ]
            columns_sql = f" ({', '.join(self.editor.quote(column) for column in columns)})"
        return ", ".join(
            f"{POSTGRESQL_PRIVILEGE_KEYWORDS[Privilege(privilege)]}{columns_sql}" for privilege in grant.privileges
        )
