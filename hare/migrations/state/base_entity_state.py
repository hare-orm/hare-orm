from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass
class BaseEntityState:
    @classmethod
    def from_dict(cls, env: Mapping[str, Any]) -> BaseEntityState:
        return cls(**{key: value for key, value in env.items() if key in inspect.signature(cls).parameters})
