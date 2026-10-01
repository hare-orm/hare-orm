from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True, slots=True)
class TextParameter:
    """Marks a type whose value arrives as one text value and is parsed by the request query - a
    framework adapter passes the raw text through instead of parsing it itself.

    Args:
        description: How the text is written, for the API schema.
    """

    description: str
