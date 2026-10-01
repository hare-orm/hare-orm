from typing import Any

from hare.dialects.postgresql.constants import IVFFLAT_LISTS_RANGE
from hare.dialects.postgresql.indexes.postgresql_index import PostgresqlIndex


class IvfflatIndex(PostgresqlIndex):
    """A pgvector IVFFlat index - ``CREATE INDEX ... USING IVFFLAT (...) WITH (lists=N)``.

    Args:
        lists: Number of inverted lists (clusters) to partition vectors into, 1 to 32768.

    Raises:
        ConfigurationError: ``lists`` isn't an int in that range.
    """

    INDEX_TYPE = "IVFFLAT"
    SUPPORTS_INCLUDE = False
    INTEGER_STORAGE_PARAMETERS = ("lists",)

    #: The default when `lists` isn't given. Not a pgvector-recommended value; pgvector itself
    #: suggests `rows / 1000` for up to 1M rows as a starting point.
    DEFAULT_LISTS = 100

    def __init__(self, *args: Any, lists: int = DEFAULT_LISTS, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lists = self.validate_storage_parameter("lists", lists, IVFFLAT_LISTS_RANGE)
        # Prepended: storage parameters come before a partial index's WHERE.
        self.extra = f" WITH (lists={self.lists}){self.extra}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        if self.lists != self.DEFAULT_LISTS:
            kwargs["lists"] = self.lists
        return path, args, kwargs
