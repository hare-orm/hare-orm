from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hare.fields.field import Field


@dataclass(frozen=True, slots=True)
class ResultLiteralBinding:
    """A selected literal's prepared conversion and matching SQL/value metadata.

    The containing expression selects the encoder once. Keeping its field and SQL
    type together prevents later typing from mistaking encoded text for plain text.
    """

    encoder: Callable[[Any], Any]
    output_field: Field[Any]
    cast_type: str | None
