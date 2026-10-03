from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext
from hare.sql.terms.base.literal_value import LiteralValue
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    pass
from hare.sql.terms.tuple import Tuple


class Array(Tuple):
    def __init__(self, *values: Any) -> None:
        # Not Tuple.__init__: the parameterized render reads only original_value, and wrapping every
        # element up front cost 97% of building a large list.
        Term.__init__(self)
        self.original_value = list(values)
        self._values: list[LiteralValue | Tuple | ValueWrapper] | None = None

    @property
    def values(self) -> list[LiteralValue | Tuple | ValueWrapper]:
        if self._values is None:
            self._values = [self.wrap_constant(value) for value in self.original_value]
        return self._values

    @values.setter
    def values(self, new_values: list[LiteralValue | Tuple | ValueWrapper]) -> None:
        self._values = new_values

    def get_sql(self, ctx: SqlContext) -> str:
        if ctx.parameterizer is None or not ctx.parameterizer.should_parameterize(self.original_value):
            if ctx.parameterizer is not None:
                ctx.parameterizer.record_literal(self)
            sql = ctx.dialect.get_array_literal_sql([term.get_sql(ctx) for term in self.values])
            return ctx.format_alias_sql(sql, self.alias)

        param = ctx.parameterizer.create_param(self.original_value, self)
        return ctx.format_alias_sql(param.get_sql(ctx), self.alias)
