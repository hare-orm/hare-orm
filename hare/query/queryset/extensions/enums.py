from enum import StrEnum


class ExtensionPhase(StrEnum):
    """Where a SQL extension is applied when a query needs a derived row source."""

    ROW_SOURCE = "row_source"
    STATEMENT = "statement"
