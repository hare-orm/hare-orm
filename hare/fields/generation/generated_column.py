from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError, UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field


@dataclass(frozen=True)
class GeneratedColumn:
    """The expression and storage of a computed column, independently of its public field type."""

    expression: Any
    stored: bool = True

    def get_field_names(self) -> tuple[str, ...]:
        """Read structural field references; raw SQL remains opaque."""
        from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming

        return tuple(sorted(ExpressionFieldRenaming.get_all_read_field_names(self.expression)))

    def with_renamed_field(self, old_name: str, new_name: str) -> GeneratedColumn:
        """Return a declaration with a renamed reference, preserving literal and raw text."""
        from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming

        expression = ExpressionFieldRenaming.get_renamed(self.expression, old_name, new_name)
        return self if expression is self.expression else GeneratedColumn(expression, self.stored)

    def get_sql(self, field: Field[Any], dialect: Dialect) -> str:
        """Render through the dialect's generated-column template and normal DDL expression path."""
        from hare.ddl.expressions.declared_expressions import DeclaredExpressions
        from hare.ddl.raw_sql_term import RawSQLTerm

        if not self.stored and not dialect.features.supports_virtual_generated_columns:
            raise UnSupportedError(
                f"Generated column {field.model_field_name!r} has stored=False, but {dialect} "
                "only supports STORED generated columns."
            )
        if field.model_field_name in self.get_field_names():
            raise ConfigurationError(f"Generated column {field.model_field_name!r} reads itself.")
        expression = self.expression
        if isinstance(expression, dict):
            expression = dialect.get_declared_variant(expression)
            if expression is None:
                raise ConfigurationError(
                    f"Generated column {field.model_field_name!r} has no expression for {dialect}."
                )
        sql = (
            expression.sql
            if isinstance(expression, RawSQLTerm)
            else DeclaredExpressions.get_sql(expression, field.model, dialect)
        )
        schema_editor = dialect.schema_editor_class
        template = (
            schema_editor.STORED_GENERATED_COLUMN_TEMPLATE
            if self.stored
            else schema_editor.VIRTUAL_GENERATED_COLUMN_TEMPLATE
        )
        return template.format(expression=sql)
