from hare.sql.context import SqlContext  # noqa: E402
from hare.sql.sql_type_length import SqlTypeLength


class SqlType:
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, length: int) -> SqlTypeLength:
        return SqlTypeLength(self.name, length)

    def get_sql(self, ctx: SqlContext) -> str:
        return f"{self.name}"
