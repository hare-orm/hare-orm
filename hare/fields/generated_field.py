from __future__ import annotations

from collections.abc import Callable
from copy import copy
from typing import TYPE_CHECKING, Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ConfigurationError
from hare.fields.field import Field
from hare.fields.generation.generated_column import GeneratedColumn

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
            mapping of dialect names to ``RawSQLTerm``, or has no entry for the dialect.
        UnSupportedError: ``stored=False`` on a dialect without ``VIRTUAL`` columns.
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
            raise ConfigurationError(
                "GeneratedField can't be primary_key=True - computed columns are not supported "
                "as model primary keys. Use a plain field for the primary key and put this "
                "expression on a separate column."
            )
        if kwargs.get("validators"):
            raise ConfigurationError(
                "GeneratedField can't take validators= - its value is computed by the database, "
                "so write validators do not run for it. Put validators on the expression's "
                "input fields or declare a database constraint on the computed result."
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

    def get_generated_sql(self, dialect: Dialect) -> str:
        return GeneratedColumn(self.expression, self.stored).get_sql(self, dialect)

    def get_python_type(self) -> Any:
        return self.output_field.get_python_type()

    def get_value_annotation(self) -> Any:
        return self.output_field.get_value_annotation()

    def get_json_value(self, value: Any) -> Any:
        """The output field's JSON form, including its range, container or other custom encoding."""
        return self.output_field.get_json_value(value)

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
        return GeneratedColumn(self.expression, self.stored).get_field_names()

    def with_renamed_generated_from_field(self, old_name: str, new_name: str) -> Field[Any]:
        declaration = GeneratedColumn(self.expression, self.stored)
        renamed = declaration.with_renamed_field(old_name, new_name)
        if renamed is declaration:
            return self
        renamed_field = copy(self)
        renamed_field.expression = renamed.expression
        return renamed_field

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        # __init__ passes generated=True itself - the deconstructed kwarg would collide.
        path, args, kwargs = super().deconstruct()
        kwargs.pop("generated", None)
        return path, args, kwargs
