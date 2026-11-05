from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Measurement:
    """Raw observations of one scenario; samples and units survive report generation.

    ``statistic`` specifies how a process summarizes its samples. Repeated process
    summaries are aggregated by their median, without discarding the raw samples.
    ``warmup_operations`` counts untimed calls; preparation is also always untimed.
    """

    samples: tuple[float, ...]
    unit: str = "ms"
    statistic: str = "median"
    warmup_operations: int = 0
    conditions: dict[str, Any] = field(default_factory=dict)
