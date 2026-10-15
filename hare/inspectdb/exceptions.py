from __future__ import annotations

from typing import Any

from hare.exceptions import ConfigurationError, OperationalError, UnSupportedError


class ManyToManyThroughTableSkippedError(ConfigurationError):
    def __init__(self, table_name: str) -> None:
        self.table_name = table_name
        super().__init__(
            f"inspectdb: table {table_name!r} looks like a ManyToManyField through table (every "
            "column is a foreign key, no other columns) and skip_many_to_many_through_tables=True - pass "
            "skip_many_to_many_through_tables=False to build a model for it anyway."
        )

    def __reduce__(self) -> tuple[type[ManyToManyThroughTableSkippedError], tuple[Any, ...]]:
        return self.__class__, (self.table_name,)


class DuplicateModelClassNameError(ConfigurationError):
    def __init__(self, first_table: str, second_table: str, class_name: str) -> None:
        self.first_table = first_table
        self.second_table = second_table
        self.class_name = class_name
        super().__init__(
            f"inspectdb: tables {first_table!r} and {second_table!r} both derive the Python class "
            f"name {class_name!r} - rename one of the tables, or inspect them in separate "
            "inspectdb runs, to avoid the generated source silently defining two classes with the "
            "same name (the second would shadow the first, and that table's model would vanish "
            "with no error)."
        )

    def __reduce__(self) -> tuple[type[DuplicateModelClassNameError], tuple[Any, ...]]:
        return self.__class__, (self.first_table, self.second_table, self.class_name)


class SchemaNotFoundError(ConfigurationError):
    def __init__(self, schema: str) -> None:
        self.schema = schema
        super().__init__(f"inspectdb: schema {schema!r} doesn't exist")

    def __reduce__(self) -> tuple[type[SchemaNotFoundError], tuple[Any, ...]]:
        return self.__class__, (self.schema,)


class TableNotFoundError(OperationalError):
    def __init__(self, table: str) -> None:
        self.table = table
        super().__init__(f"inspectdb: table {table!r} doesn't exist")

    def __reduce__(self) -> tuple[type[TableNotFoundError], tuple[Any, ...]]:
        return self.__class__, (self.table,)


class UnsupportedDialectError(UnSupportedError):
    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        super().__init__(f"The {dialect} dialect has no schema introspector - inspectdb can't read its databases")

    def __reduce__(self) -> tuple[type[UnsupportedDialectError], tuple[Any, ...]]:
        return self.__class__, (self.dialect,)
