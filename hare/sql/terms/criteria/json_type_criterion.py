from __future__ import annotations

from hare.sql.terms.criteria.json_attribute_criterion import JSONAttributeCriterion
from hare.sql.terms.term import Term


class JSONTypeCriterion(JSONAttributeCriterion):
    """The JSON type name of the value at a path, as SQLite's ``json_type()`` spells it
    (``null``, ``true``, ``false``, ``integer``, ``real``, ``text``, ``array``, ``object``), or
    NULL for a missing path. SQLite only."""

    def __init__(self, json_column: Term, path: list[str | int], alias: str | None = None) -> None:
        super().__init__(json_column, path, alias, as_text=False)
