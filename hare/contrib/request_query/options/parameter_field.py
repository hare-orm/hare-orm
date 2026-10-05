from __future__ import annotations

import dataclasses
from typing import Any

from hare.contrib.request_query.enums import ParameterType


@dataclasses.dataclass(frozen=True, slots=True)
class ParameterField:
    """A parameter an option adds to a request query.

    Attributes:
        name: The parameter's name.
        annotation: Its type.
        default: Its default - a value, or a pydantic ``Field`` with the default and a description.
        parameter_type: What the parameter does - ``ParameterType.OPTION`` for an option of the
            application's own.
        allowed_values: The names a request may give it (orderings, fields, relations), None when
            it takes any value of its type.
    """

    name: str
    annotation: Any
    default: Any
    parameter_type: ParameterType = ParameterType.OPTION
    allowed_values: tuple[str, ...] | None = None
