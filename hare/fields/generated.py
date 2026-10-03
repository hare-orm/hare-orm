from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.base.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.sql.terms.base.term import Term


class GeneratedField(Field[Any]):
    """A column the database computes from a raw SQL expression over the row (``GENERATED ALWAYS AS
    (...) STORED``/``VIRTUAL``) - right for every write the database sees, outside hare too.
    Postgres has only ``STORED``; SQLite (3.31+) both.

    Args:
        expression: The raw SQL expression (``"price * quantity"``), or a ``{"sqlite": ...,
            "postgresql": ...}`` mapping.
        output_field: A field giving the column's type (``DecimalField(max_digits=10,
            decimal_places=2)``).
        stored: ``STORED`` (computed on write) or ``VIRTUAL`` (on read).

    Raises:
        ConfigurationError: ``expression`` has no entry for the dialect, or ``stored=False`` on a
            dialect without ``VIRTUAL`` columns.
    """

    allows_generated = True

    def __init__(
        self,
        expression: str | dict[str, str],
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
        # A column computed from secret data is itself secret unless explicitly marked otherwise.
        kwargs.setdefault("sensitive", output_field.sensitive)
        super().__init__(generated=True, **kwargs)
        self.expression = expression
        self.output_field = output_field
        self.stored = stored
        self.field_type = output_field.field_type

    def _expression_for_dialect(self, dialect: Dialect) -> str:
        if isinstance(self.expression, str):
            return self.expression
        try:
            return self.expression[dialect.name]
        except KeyError:
            raise ConfigurationError(
                f"GeneratedField '{self.model_field_name}' has no expression for dialect '{dialect}'."
            ) from None

    def get_generated_sql(self, dialect: Dialect) -> str:
        if not self.stored and not dialect.supports_virtual_generated_columns:
            raise UnSupportedError(
                f"GeneratedField '{self.model_field_name}' has stored=False, but the {dialect} dialect "
                "only supports STORED generated columns."
            )
        expression = self._expression_for_dialect(dialect)
        mode = "STORED" if self.stored else "VIRTUAL"
        return f"GENERATED ALWAYS AS ({expression}) {mode}"

    def get_python_type(self) -> Any:
        return self.output_field.get_python_type()

    def get_value_annotation(self) -> Any:
        return self.output_field.get_value_annotation()

    def get_column_type(self, dialect: Dialect) -> str:
        return self.output_field.get_column_type(dialect)

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

    @property
    def requires_extension(self) -> str | None:  # type: ignore[override]
        return self.output_field.requires_extension

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        # __init__ passes generated=True itself - the deconstructed kwarg would collide.
        path, args, kwargs = super().deconstruct()
        kwargs.pop("generated", None)
        return path, args, kwargs
