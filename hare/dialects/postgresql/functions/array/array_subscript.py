from typing import Any

from hare.dialects.postgresql.constants import (
    ARRAY_ITEM_INDEX_MAX,
    ARRAY_ITEM_INDEX_MIN,
    ARRAY_OUTER_BRACES_PATTERN,
    ARRAY_OUTER_BRACES_REPLACEMENT,
)
from hare.dialects.postgresql.fields.array import ArrayField
from hare.exceptions import ValidationError
from hare.sql.context import SqlContext
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.field import Field as ColumnTerm
from hare.sql.terms.functions.function import Function


class ArraySubscript(Function):
    """``array_expr[index]`` - the array subscript, rendered instead of a function call.

    Args:
        term: The array expression.
        index: The 0-based position, rendered as SQL text. A negative one counts from the end of the
            array.
        subarray_field: The element's own array field when the array is nested - a row is taken as a
            one-row slice and cast back to the field's type.
    """

    def __init__(
        self, term: Any, index: int, subarray_field: ArrayField | None = None, alias: str | None = None
    ) -> None:
        super().__init__("array_subscript", term, alias=alias)
        self.index = self.get_validated_index(index)
        self.subarray_field = subarray_field

    @staticmethod
    def get_validated_index(index: Any) -> int:
        """Checks an array position before it's rendered into the SQL text as a literal.

        Args:
            index: The 0-indexed position.

        Returns:
            The position as a plain ``int``.

        Raises:
            ValidationError: If ``index`` isn't an ``int`` (a ``bool`` included) or lies outside
                the range a Postgres array subscript can address.
        """
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValidationError(f"Array item index must be an int, got {type(index).__name__}")
        if not ARRAY_ITEM_INDEX_MIN <= index <= ARRAY_ITEM_INDEX_MAX:
            raise ValidationError(
                f"Array item index must be between {ARRAY_ITEM_INDEX_MIN} and {ARRAY_ITEM_INDEX_MAX}, got {index}"
            )
        return int(index)

    @classmethod
    def get_array_sql(cls, term: Any, ctx: SqlContext) -> str:
        """The SQL of an indexed array - parenthesized unless it's a plain column, since a
        subscript directly after a function call or cast is a syntax error.

        Args:
            term: The array-valued expression.
            ctx: The rendering context.

        Returns:
            The SQL to append a subscript to.
        """
        array_sql = Function.get_arg_sql(term, ctx)
        return array_sql if isinstance(term, ColumnTerm) else f"({array_sql})"

    def get_sql(self, ctx: SqlContext) -> str:
        arg_ctx = ctx.copy(with_alias=False)
        array_sql = self.get_array_sql(self.args[0], arg_ctx)
        if self.index >= 0:
            subscript_sql = str(self.index + 1)
        else:
            subscript_sql = f"(array_length({array_sql}, 1) + {self.index + 1})"
        if self.subarray_field is None:
            sql = f"{array_sql}[{subscript_sql}]"
        else:
            # The pattern and replacement are bound - their backslashes would read as escapes in a
            # literal under standard_conforming_strings=off.
            pattern_sql = ValueWrapper(ARRAY_OUTER_BRACES_PATTERN).get_sql(arg_ctx)
            replacement_sql = ValueWrapper(ARRAY_OUTER_BRACES_REPLACEMENT).get_sql(arg_ctx)
            row_text_sql = (
                f"regexp_replace(CAST({array_sql}[{subscript_sql}:{subscript_sql}] AS TEXT),"
                f"{pattern_sql},{replacement_sql})"
            )
            sql = f"CAST(NULLIF({row_text_sql},'') AS {self.subarray_field.get_column_type(ctx.dialect)})"
        return ctx.format_alias_sql(sql, self.alias)
