from __future__ import annotations

import inspect
from typing import Any


class FieldDescriptions:
    """The descriptions of a pydantic model and its fields: a docstring cleaned of its indentation, and
    newlines written as HTML line breaks."""

    @staticmethod
    def get_clean_docstring(obj: Any) -> str:
        return FieldDescriptions.replace_newlines_with_br(inspect.cleandoc(obj.__doc__ or ""))

    @staticmethod
    def replace_newlines_with_br(text: str) -> str:
        return text.replace("\n", "<br/>").strip()
