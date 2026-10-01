from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass(slots=True)
class PlanContext:
    """What describing a part of a query needs from the query it is in.

    Never changed once made, like ``PlanDescription``.

    Args:
        annotations: The annotations the part resolves names against, None when it resolves
            none - building a part that names an annotation resolves the annotation again,
            recording its values again.
        single_parameter_in_list_min_length: The dialect's length from which an ``__in`` list
            binds as one parameter (``Dialect``) - the description of such a list holds no length.
            None to describe every list by its length.
    """

    annotations: Mapping[str, Any] | None = None
    single_parameter_in_list_min_length: int | None = None

    #: The context of a part that resolves no annotation names.
    EMPTY: ClassVar[PlanContext]


PlanContext.EMPTY = PlanContext()
