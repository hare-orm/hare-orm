from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable
from typing import Any


@dataclasses.dataclass
class ComputedFieldDescription:
    function: Callable[..., Any] | property | functools.cached_property[Any]
    description: str | None
