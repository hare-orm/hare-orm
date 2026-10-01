from hare.sql.context import SqlContext
from hare.sql.terms.base.value_wrapper import ValueWrapper


class SqliteValueWrapper(ValueWrapper):
    def get_value_sql(self, ctx: SqlContext) -> str:
        if isinstance(self.value, bool):
            return "1" if self.value else "0"
        return super().get_value_sql(ctx)
