from enum import StrEnum


class StatementForm(StrEnum):
    """The declared driver call contract, independent of its Python method name."""

    SQL = "sql"
    MANY = "many"
    SCRIPT = "script"
    BULK_LOAD = "bulk_load"
    CONTROL = "control"
