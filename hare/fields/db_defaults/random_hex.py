from __future__ import annotations

from hare.exceptions import UnSupportedError
from hare.fields.db_defaults.sql_default import SqlDefault


class RandomHex(SqlDefault):
    """A random 32-character hex string as a ``db_default``. Standard SQL has no such expression - a
    dialect registers a renderer for it.

    Example::

        class MyModel(Model):
            tracking_id = fields.CharField(max_length=36, db_default=RandomHex())
    """

    def __init__(self) -> None:
        super().__init__("")

    def get_standard_sql(self) -> str:
        raise UnSupportedError("RandomHex() has no standard SQL - the dialect has no renderer for it")

    def __repr__(self) -> str:
        return "RandomHex()"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RandomHex)

    def __hash__(self) -> int:
        return hash("RandomHex")
