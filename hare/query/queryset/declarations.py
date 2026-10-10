from __future__ import annotations

from typing import TypedDict


class NoAnnotations(TypedDict):
    """The annotations of a queryset without ``.annotate()``/``.alias()`` expressions - none."""
