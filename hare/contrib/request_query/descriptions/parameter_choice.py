from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True, slots=True)
class ParameterChoice:
    """One value a parameter may take, with its label - a member of the enum it takes.

    Attributes:
        value: The value a request gives.
        label: The member's name.
    """

    value: Any
    label: str
