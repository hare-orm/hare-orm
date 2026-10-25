from __future__ import annotations

from collections.abc import Callable
from copy import copy
from typing import TYPE_CHECKING, Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.field import Field

if TYPE_CHECKING:
    from hare.ddl.raw_sql_term import RawSQLTerm
    from hare.ddl.schema_objects.enum_type import EnumType
    from hare.dialects.base.dialect import Dialect
    from hare.query.expressions.expression import Expression
    from hare.sql.terms.term import Term


class GeneratedField(Field[Any]):
    """A column the database computes from an expression over the row (``GENERATED ALWAYS AS
    (...) STORED``/``VIRTUAL``, or a dialect's own words for them) - right for every write the
    database sees, outside hare too. ``VIRTUAL`` needs ``features.supports_virtual_generated_columns``.

    Args:
        expression: An expression of hare over the model's fields (``F("price") * F("quantity")``),
            written in each database's own SQL and renamed with the fields it reads; or ``RawSQLTerm``
            of the SQL expression (``RawSQLTerm("price * quantity")``), or a ``{"sqlite": RawSQLTerm(...),
            "postgresql": RawSQLTerm(...)}`` mapping by dialect - written as it is.
        output_field: A field giving the column's type (``DecimalField(max_digits=10,
            decimal_places=2)``).
        stored: ``STORED`` (computed on write) or ``VIRTUAL`` (on read).

    Raises:
        ConfigurationError: ``expression`` is neither an expression of hare, a ``RawSQLTerm`` nor a
            mapping of dialect names to ``RawSQLTerm``, or has no entry for the dialect, or
            ``stored=False`` on a dialect without ``VIRTUAL`` columns.
    """

    allows_generated = True

    def __init__(
        self,
        expression: Expression | RawSQLTerm | dict[str, RawSQLTerm],
        output_field: Field[Any],
        stored: bool = True,
        **kwargs: Any,
    ) -> None:
        if kwargs.get("primary_key") or kwargs.get("pk"):
            # A generated column can't be the primary key: the DDL of a primary key column expects
            # an auto-increment definition.
            raise ConfigurationError(
                "GeneratedField can't be primary_key=True - every DDL path that renders a "
                "primary key column assumes an auto-increment integer PK's self-contained "
                "GENERATED_SQL (already including its own type and PRIMARY KEY marker), which "
                "this class's GENERATED_SQL (just the bare GENERATED ALWAYS AS (...) clause) "
                "doesn't provide. Use a plain field for the primary key and put this expression "
                "on a separate, non-pk column instead."
            )
        if kwargs.get("validators"):
            # The application never writes this column, so validators here would never run - they go
            # on output_field.
            raise ConfigurationError(
                "GeneratedField can't take validators= - its value is computed by the database, "
                "never assigned by the application, so there's nothing to validate. Put "
                "validators on output_field= instead if you need to validate one of the "
                "expression's own input columns."
            )
        # Local import: the ddl package imports the query package, which imports the fields.
        from hare.ddl.raw_sql_term import RawSQLTerm
        from hare.query.expressions.expression import Expression

        expressions = expression.values() if isinstance(expression, dict) else [expression]
        if not isinstance(expression, Expression) and (
            (
                isinstance(expression, dict)
                and (not expression or not all(isinstance(dialect_name, str) for dialect_name in expression))
            )
            or not all(isinstance(term, RawSQLTerm) and term.sql.strip() for term in expressions)
        ):
            raise ConfigurationError(
                "GeneratedField.expression takes an expression of hare (F('price') * F('quantity')), "
                "RawSQLTerm(...) of raw SQL, or a mapping of dialect names to RawSQLTerm(...), got "
                f"{ValueDescriptions.get_shown_value(expression)}"
            )
        # A column computed from secret data is itself secret unless explicitly marked otherwise.
        kwargs.setdefault("sensitive", output_field.sensitive)
        super().__init__(generated=True, **kwargs)
        self.expression = expression
        self.output_field = output_field
        self.stored = stored
        self.field_type = output_field.field_type

    @staticmethod
    def get_effective_field(field: Field[Any]) -> Field[Any]:
        """The field a value of ``field`` has: a GeneratedField's output field, any other field
        itself.

        Args:
            field: The field.

        Returns:
            The field the value has.
        """
        return field.output_field if isinstance(field, GeneratedField) else field

    def _expression_for_dialect(self, dialect: Dialect) -> str:
        # Local import: the ddl package imports the query package, which imports the fields.
        from hare.ddl.expressions.declared_expressions import DeclaredExpressions
        from hare.ddl.raw_sql_term import RawSQLTerm

        if isinstance(self.expression, RawSQLTerm):
            return self.expression.sql
        if not isinstance(self.expression, dict):
            return DeclaredExpressions.get_sql(self.expression, self.model, dialect)
        expression = dialect.get_declared_variant(self.expression)
        if expression is None:
            raise ConfigurationError(
                f"GeneratedField {self.model_field_name!r} has no expression for dialect {str(dialect.name)!r}."
            )
        return expression.sql

    def get_generated_sql(self, dialect: Dialect) -> str:
        if not self.stored and not dialect.features.supports_virtual_generated_columns:
            raise UnSupportedError(
                f"GeneratedField {self.model_field_name!r} has stored=False, but the {dialect} dialect "
                "only supports STORED generated columns."
            )
        if self.model_field_name in self.get_generated_from_field_names():
            # A column computed from itself - a database refuses its table, or every row written.
            raise ConfigurationError(
                f"GeneratedField {self.model_field_name!r} reads itself - a column can't be computed from its "
                "own value."
            )
        expression = self._expression_for_dialect(dialect)
        schema_editor_class = dialect.schema_editor_class
        template = (
            schema_editor_class.STORED_GENERATED_COLUMN_TEMPLATE
            if self.stored
            else schema_editor_class.VIRTUAL_GENERATED_COLUMN_TEMPLATE
        )
        return template.format(expression=expression)

    def get_python_type(self) -> Any:
        return self.output_field.get_python_type()

    def get_value_annotation(self) -> Any:
        return self.output_field.get_value_annotation()

    def get_column_type(self, dialect: Dialect) -> str:
        return self.output_field.get_column_type(dialect)

    def exists_on(self, dialect: Dialect) -> bool:
        return self.output_field.exists_on(dialect)

    def get_function_cast(self, dialect: Dialect) -> Callable[[Field[Any], Term], Term] | None:
        """The cast of ``output_field`` on ``dialect`` - the column compares and orders the way a
        column of the output field's own type does.

        Returns:
            A ``(field, term) -> term`` cast, or ``None`` when the output field has none.
        """
        output_function_cast = self.output_field.get_function_cast(dialect)
        if output_function_cast is None:
            return None
        output_field = self.output_field
        return lambda _field, term: output_function_cast(output_field, term)

    def to_db_value(self, value: Any, instance: Any) -> Any:
        return self.output_field.to_db_value(value, instance)

    def from_db_value(self, value: Any) -> Any:
        return self.output_field.from_db_value(value)

    def to_python(self, value: Any) -> Any:
        return self.output_field.to_python(value)

    def get_assign_normalized_types(self) -> frozenset[type]:
        return self.output_field.get_assign_normalized_types()

    @property
    def constraints(self) -> dict[str, Any]:
        return self.output_field.constraints

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        return self.output_field.SQL_TYPE

    def get_required_extension(self, dialect: Dialect) -> str | None:
        return self.output_field.get_required_extension(dialect)

    @property
    def requires_enum_type(self) -> EnumType | None:  # type: ignore[override]
        return self.output_field.requires_enum_type

    def get_generated_from_field_names(self) -> tuple[str, ...]:
        # Local import: the ddl package imports the query package, which imports the fields.
        from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming

        # A raw SQL expression's fields aren't read out of it.
        return tuple(sorted(ExpressionFieldRenaming.get_all_read_field_names(self.expression)))

    def with_renamed_generated_from_field(self, old_name: str, new_name: str) -> Field[Any]:
        # Local import: the ddl package imports the query package, which imports the fields.
        from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming

        renamed_expression = ExpressionFieldRenaming.get_renamed(self.expression, old_name, new_name)
        if renamed_expression is self.expression:
            return self
        renamed_field = copy(self)
        renamed_field.expression = renamed_expression
        return renamed_field

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        # __init__ passes generated=True itself - the deconstructed kwarg would collide.
        path, args, kwargs = super().deconstruct()
        kwargs.pop("generated", None)
        return path, args, kwargs
