from __future__ import annotations

from enum import StrEnum


class VectorDistanceType(StrEnum):
    """What a vector distance measures."""

    #: Euclidean distance.
    L2 = "l2"
    #: Cosine distance.
    COSINE = "cosine"
    #: The inner product, negated - smaller is more similar, as for the other two.
    NEGATIVE_INNER_PRODUCT = "negative_inner_product"
