from hare.sql.context import SqlContext  # noqa: E402


class SqlTypeLength:
    def __init__(self, name: str, length: int) -> None:
        self.name = name
        self.length = length

    def get_sql(self, ctx: SqlContext) -> str:
        return f"{self.name}({self.length})"
