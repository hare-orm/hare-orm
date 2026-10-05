from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.vectors.enums import VectorDistanceType
from hare.vectors.vector_distance_expression import VectorDistanceExpression

L2Distance = DeclaredSubclass.make(
    VectorDistanceExpression,
    "L2Distance",
    __name__,
    """Euclidean (L2) distance.

    Example: ``Item.objects.annotate(dist=L2Distance("embedding",
    query_vector)).order_by("dist").limit(10)``""",
    DISTANCE_TYPE=VectorDistanceType.L2,
)


CosineDistance = DeclaredSubclass.make(
    VectorDistanceExpression,
    "CosineDistance",
    __name__,
    """Cosine distance.

    Example: ``Item.objects.annotate(dist=CosineDistance("embedding",
    query_vector)).order_by("dist").limit(10)``""",
    DISTANCE_TYPE=VectorDistanceType.COSINE,
)


InnerProduct = DeclaredSubclass.make(
    VectorDistanceExpression,
    "InnerProduct",
    __name__,
    """The NEGATIVE inner product - so smaller is more similar, as for the other two distances, and
    ``.order_by("dist")`` puts the most similar rows first.

    Example: ``Item.objects.annotate(dist=InnerProduct("embedding",
    query_vector)).order_by("dist").limit(10)``""",
    DISTANCE_TYPE=VectorDistanceType.NEGATIVE_INNER_PRODUCT,
)
