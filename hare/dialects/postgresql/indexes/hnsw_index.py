from typing import Any

from hare.dialects.postgresql.constants import HNSW_EF_CONSTRUCTION_RANGE, HNSW_M_RANGE
from hare.dialects.postgresql.indexes.postgresql_index import PostgresqlIndex
from hare.exceptions import ConfigurationError


class HnswIndex(PostgresqlIndex):
    """A pgvector HNSW index - ``CREATE INDEX ... USING HNSW (...) WITH (m=M, ef_construction=N)``.

    Args:
        m: Max number of connections per graph layer, 2 to 100.
        ef_construction: Size of the dynamic candidate list used while building the graph, 4 to
            1000 and at least ``2 * m``.

    Raises:
        ConfigurationError: If ``fields=`` is given without ``opclasses=`` - pgvector has no
            default HNSW operator class for any of its types, so the distance
            (``vector_l2_ops``, ``vector_cosine_ops``, ...) must be named - or ``m``/
            ``ef_construction`` isn't an int in its range.
    """

    INDEX_TYPE = "HNSW"
    SUPPORTS_INCLUDE = False
    INTEGER_STORAGE_PARAMETERS = ("m", "ef_construction")

    #: The defaults when `m`/`ef_construction` aren't given - they match pgvector's own
    #: documented defaults, so a plain `CREATE INDEX ... USING hnsw (...)` (no explicit
    #: WITH (...)) behaves as these two values regardless.
    DEFAULT_M = 16
    DEFAULT_EF_CONSTRUCTION = 64

    def __init__(
        self, *args: Any, m: int = DEFAULT_M, ef_construction: int = DEFAULT_EF_CONSTRUCTION, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        if self.fields and not self.opclasses:
            raise ConfigurationError(
                "HnswIndex requires opclasses= (e.g. opclasses=('vector_l2_ops',)) - pgvector has no "
                "default operator class for the hnsw access method."
            )
        self.m = self.validate_storage_parameter("m", m, HNSW_M_RANGE)
        self.ef_construction = self.validate_storage_parameter(
            "ef_construction", ef_construction, HNSW_EF_CONSTRUCTION_RANGE
        )
        if self.ef_construction < 2 * self.m:
            raise ConfigurationError(
                f"HnswIndex ef_construction must be at least 2 * m ({2 * self.m}), got {self.ef_construction}"
            )
        # Same WITH-before-WHERE ordering trap as IvfflatIndex above.
        self.extra = f" WITH (m={self.m}, ef_construction={self.ef_construction}){self.extra}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        if self.m != self.DEFAULT_M:
            kwargs["m"] = self.m
        if self.ef_construction != self.DEFAULT_EF_CONSTRUCTION:
            kwargs["ef_construction"] = self.ef_construction
        return path, args, kwargs
