from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.context import SqlContext

if TYPE_CHECKING:
    pass
from hare.sql.terms.base.value_wrapper import ValueWrapper


class ParameterizedValueWrapper(ValueWrapper):
    """A ``ValueWrapper`` of a converted scalar that is always parameterized - skips
    ``should_parameterize()``, a real cost for a long ``IN`` list. The parameter is still created at
    render time; without a parameterizer the value is rendered as a literal.
    """

    def get_sql(self, ctx: SqlContext) -> str:
        if ctx.parameterizer is None:
            return super().get_sql(ctx)
        param = ctx.parameterizer.create_param(
            self.value, self.parameter_source or self, reuse=ctx.dialect.numbers_parameters
        )
        return ctx.format_alias_sql(param.get_sql(ctx), self.alias)
