from __future__ import annotations

from dataclasses import dataclass

from hare.sql.terms.values.value_wrapper import ValueWrapper


@dataclass(frozen=True, slots=True)
class LikePatternParameter:
    source: ValueWrapper
    prefix: bool
    suffix: bool
    value: str
