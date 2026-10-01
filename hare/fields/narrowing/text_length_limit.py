from __future__ import annotations

from dataclasses import dataclass

from hare.fields.enums import NarrowedValueSource


@dataclass(frozen=True)
class TextLengthLimit:
    """A value's text takes no more than ``max_length`` characters."""

    max_length: int
    source: NarrowedValueSource
