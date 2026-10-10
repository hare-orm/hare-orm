from __future__ import annotations

from hare.exceptions import ConfigurationError, OperationalError, UnSupportedError


class ManyToManyThroughTableSkippedError(ConfigurationError):
    def __init__(self, table_name: str) -> None:
        super().__init__(
            f"inspectdb: table {table_name!r} looks like a ManyToManyField through table (every "
            "column is a foreign key, no other columns) and skip_many_to_many_through_tables=True - pass "
            "skip_many_to_many_through_tables=False to build a model for it anyway."
        )


class DuplicateModelClassNameError(ConfigurationError):
    def __init__(self, first_table: str, second_table: str, class_name: str) -> None:
        super().__init__(
            f"inspectdb: tables {first_table!r} and {second_table!r} both derive the Python class "
            f"name {class_name!r} - rename one of the tables, or inspect them in separate "
            "inspectdb runs, to avoid the generated source silently defining two classes with the "
            "same name (the second would shadow the first, and that table's model would vanish "
            "with no error)."
        )


class SchemaNotFoundError(ConfigurationError):
    def __init__(self, schema: str) -> None:
        super().__init__(f"inspectdb: schema {schema!r} doesn't exist")


class TableNotFoundError(OperationalError):
    def __init__(self, table: str) -> None:
        super().__init__(f"inspectdb: table {table!r} doesn't exist")


class UnsupportedDialectError(UnSupportedError):
    def __init__(self, dialect: str) -> None:
        super().__init__(f"The {dialect} dialect has no schema introspector - inspectdb can't read its databases")
