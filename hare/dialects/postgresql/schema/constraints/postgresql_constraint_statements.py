from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.base.schema.constraints.constraint_statements import ConstraintStatements
from hare.exceptions import ConfigurationError
from hare.fields.field import Field
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlConstraintStatements(ConstraintStatements):
    """ConstraintStatements as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def exclusion_constraint_sql(self, model: type[Model], constraint: ExclusionConstraint) -> str:
        expression_sqls = []
        for expression, operator in constraint.expressions:
            # A field name is its quoted column; a RawSQLTerm is spliced in as written.
            expression_sql = (
                expression.get_sql()
                if isinstance(expression, RawSQLTerm)
                else self.editor.quote(self.editor.constraint_names.get_fields_to_columns(model, [expression])[0])
            )
            expression_sqls.append(f"{expression_sql} WITH {operator}")
        clauses_sql = ""
        if constraint.include:
            clauses_sql += self.editor.index_statements.get_index_include_sql(
                [self.editor.quote(column) for column in model._meta.get_column_names(constraint.include)]
            )
        if constraint.condition:
            clauses_sql += f" WHERE ({ConstraintCondition.get_sql(constraint.condition, model, self.editor.client)})"
        if constraint.deferrable:
            clauses_sql += " DEFERRABLE INITIALLY " + ("DEFERRED" if constraint.initially_deferred else "IMMEDIATE")
        return self.editor.EXCLUSION_CONSTRAINT_CREATE_TEMPLATE.format(
            name=self.editor.quote(constraint.name),
            using=constraint.using,
            expressions=", ".join(expression_sqls),
            where=clauses_sql,
        )

    async def add_check_constraint_not_valid(self, model: type[Model], constraint: CheckConstraint) -> None:
        constraint_sql = self.editor.CHECK_CONSTRAINT_CREATE_TEMPLATE.format(
            name=self.editor.quote(constraint.name),
            check=ConstraintCondition.get_sql(constraint.check, model, self.editor.client),
        )
        await self.editor.run_sql(
            self.editor.ADD_CONSTRAINT_TEMPLATE.format(
                table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
                constraint=f"{constraint_sql} NOT VALID",
            )
        )

    async def add_unique_constraint_using_index(
        self, model: type[Model], constraint: UniqueConstraint, index_name: str
    ) -> None:
        # The index is renamed to the constraint's name and kept.
        deferrable_sql = ""
        if constraint.deferrable:
            deferrable_sql = " DEFERRABLE INITIALLY " + ("DEFERRED" if constraint.initially_deferred else "IMMEDIATE")
        await self.editor.run_sql(
            self.editor.ADD_CONSTRAINT_TEMPLATE.format(
                table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
                constraint=(
                    f"CONSTRAINT {self.editor.quote(cast('str', constraint.name))} UNIQUE USING INDEX "
                    f"{self.editor.quote(index_name)}{deferrable_sql}"
                ),
            )
        )

    async def validate_constraint(self, model: type[Model], name: str) -> None:
        table = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        await self.editor.run_sql(f"ALTER TABLE {table} VALIDATE CONSTRAINT {self.editor.quote(name)}")

    @classmethod
    def get_nulls_distinct_sql(cls, nulls_distinct: bool) -> str:
        return " NULLS DISTINCT" if nulls_distinct else " NULLS NOT DISTINCT"

    @classmethod
    def get_exclusion_constraint_extension(
        cls, constraint: ExclusionConstraint, fields_by_name: Mapping[str, Field[Any]]
    ) -> str | None:
        """``btree_gist`` for a GiST constraint over a plain scalar column (a relation's key, a
        number, text, date, uuid, ...), which core GiST has no operator class for."""
        # Local import: the constants module instantiates this class.
        from hare.ddl.enums import ExclusionConstraintUsing
        from hare.dialects.postgresql.schema.constants import BTREE_GIST_EXTENSION

        if constraint.using != ExclusionConstraintUsing.GIST:
            return None
        for expression, _operator in constraint.expressions:
            if isinstance(expression, str) and cls.is_btree_gist_field(fields_by_name.get(expression)):
                return BTREE_GIST_EXTENSION
        return None

    @classmethod
    def get_without_overlaps_extension(
        cls, field_names: Sequence[str], fields_by_name: Mapping[str, Field[Any]]
    ) -> str | None:
        """``btree_gist`` when a scalar column comes before the range - the key is a GiST index."""
        # Local import: the constants module instantiates this class.
        from hare.dialects.postgresql.schema.constants import BTREE_GIST_EXTENSION

        if any(cls.is_btree_gist_field(fields_by_name.get(field_name)) for field_name in field_names[:-1]):
            return BTREE_GIST_EXTENSION
        return None

    @classmethod
    def is_btree_gist_field(cls, field: Field[Any] | None) -> bool:
        """Whether a field's column is a scalar type only ``btree_gist`` makes GiST-indexable.

        Args:
            field: The field, or None for an unknown name.

        Returns:
            True for a relation's key column or a column of a ``btree_gist`` type.
        """
        # Local import: the constants module instantiates this class.
        from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
        from hare.dialects.postgresql.schema.constants import BTREE_GIST_SQL_TYPES
        from hare.fields.relations.fields.relational_field import RelationalField

        if field is None:
            return False
        if isinstance(field, RelationalField):
            return True
        try:
            sql_type = field.get_column_type(POSTGRESQL_DIALECT)
        except (AttributeError, ConfigurationError):
            return False
        return isinstance(sql_type, str) and sql_type.lower().split("(")[0].strip() in BTREE_GIST_SQL_TYPES
