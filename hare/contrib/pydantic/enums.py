from __future__ import annotations

from enum import StrEnum


class FetchedFieldCheck(StrEnum):
    """What ``PydanticModel`` checks a schema field is loaded by on a model instance."""

    #: A forward or backward one-to-one relation, or a foreign key: its instance was fetched.
    RELATED_INSTANCE = "related_instance"
    #: A to-many relation: its rows were fetched.
    RELATED_ROWS = "related_rows"
    #: A column: ``.only()``/``.defer()`` didn't leave it out.
    COLUMN = "column"
