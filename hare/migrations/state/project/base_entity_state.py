from __future__ import annotations

import inspect
from dataclasses import dataclass


@dataclass
class BaseEntityState:
    @classmethod
    def from_dict(cls, env) -> BaseEntityState:
        return cls(**{k: v for k, v in env.items() if k in inspect.signature(cls).parameters})
