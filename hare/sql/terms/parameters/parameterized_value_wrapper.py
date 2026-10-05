from __future__ import annotations

from hare.sql.sql_context import SqlContext
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ParameterizedValueWrapper(ValueWrapper):
    """A ``ValueWrapper`` of a converted scalar that is always parameterized - skips
    ``should_parameterize()``, a real cost for a long ``IN`` list. The parameter is still created at
    render time; without a parameterizer the value is rendered as a literal.
    """

    def get_sql(self, sql_context: SqlContext) -> str:
        if sql_context.parameterizer is None:
            return super().get_sql(sql_context)
        parameter = sql_context.parameterizer.create_parameter(
            self.value, self.parameter_source or self, reuse=sql_context.dialect.parameters.numbers_parameters
        )
        return sql_context.format_alias_sql(parameter.get_sql(sql_context), self.alias)
