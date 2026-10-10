from __future__ import annotations

from hare.vectors.enums import VectorDistanceType

#: The extension the vector column type comes from.
PGVECTOR_EXTENSION = "vector"

#: The pgvector operator of each vector distance.
POSTGRESQL_VECTOR_DISTANCE_OPERATORS = {
    VectorDistanceType.L2: " <-> ",
    VectorDistanceType.COSINE: " <=> ",
    VectorDistanceType.NEGATIVE_INNER_PRODUCT: " <#> ",
}
